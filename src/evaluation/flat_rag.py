# Databricks notebook source
# MAGIC %md
# MAGIC ### Flat RAG Baseline
# MAGIC Embedding-based chunk retrieval pipeline for comparison with GraphRAG.

# COMMAND ----------

import mlflow
import numpy as np

# COMMAND ----------

# DBTITLE 1,Build Chapter Chunks
def build_chapter_chunks():
    """Concatenate verses into chapter-level chunks for flat RAG retrieval."""
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.getOrCreate()

    rows = spark.sql(f"""
        SELECT book, chapter,
               CONCAT(book, ' Chapter ', chapter) as chunk_id,
               CONCAT_WS(' ',
                 TRANSFORM(
                   ARRAY_SORT(COLLECT_LIST(STRUCT(verse_number, text))),
                   x -> x.text
                 )
               ) as text
        FROM {config['verses_table']}
        GROUP BY book, chapter
        ORDER BY book, chapter
    """).collect()

    return [
        {"chunk_id": r["chunk_id"], "book": r["book"], "chapter": r["chapter"], "text": r["text"]}
        for r in rows
    ]

# COMMAND ----------

# DBTITLE 1,Embed Texts via Foundation Model API
def embed_texts(texts, endpoint=None):
    """Embed a batch of texts using the Databricks embedding endpoint."""
    import mlflow.deployments
    client = mlflow.deployments.get_deploy_client("databricks")
    ep = endpoint or config['embedding_endpoint']

    all_embeddings = []
    batch_size = 16
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        response = client.predict(endpoint=ep, inputs={"input": batch})
        all_embeddings.extend([item["embedding"] for item in response.data])

    return np.array(all_embeddings)

# COMMAND ----------

# DBTITLE 1,Cosine Similarity Search
def cosine_similarity_search(query_embedding, corpus_embeddings, top_k=5):
    """Return indices and scores of the top-k most similar corpus items."""
    q_norm = query_embedding / (np.linalg.norm(query_embedding) + 1e-10)
    c_norms = corpus_embeddings / (np.linalg.norm(corpus_embeddings, axis=1, keepdims=True) + 1e-10)
    similarities = c_norms @ q_norm
    top_indices = np.argsort(similarities)[::-1][:top_k]
    return top_indices, similarities[top_indices]

# COMMAND ----------

# DBTITLE 1,Flat RAG Pipeline Class
FLAT_RAG_SYSTEM_PROMPT = """You are a biblical scholar. Answer the question using ONLY the Bible passages provided below.
If the passages do not contain enough information, say so clearly.
Always cite specific passages (Book Chapter:Verse) when possible.

Retrieved Passages:
{context}"""


class FlatRAGPipeline:
    """Embedding-based flat RAG: chunk → embed → retrieve → generate."""

    def __init__(self, llm_endpoint=None, embedding_endpoint=None, top_k=5):
        self.llm_endpoint = llm_endpoint or config['llm_endpoint']
        self.embedding_endpoint = embedding_endpoint or config['embedding_endpoint']
        self.top_k = top_k
        self.chunks = None
        self.embeddings = None

    def build_index(self):
        self.chunks = build_chapter_chunks()
        texts = [c["text"] for c in self.chunks]
        self.embeddings = embed_texts(texts, self.embedding_endpoint)
        print(f"Indexed {len(self.chunks)} chapter chunks (embedding dim={self.embeddings.shape[1]})")

    @mlflow.trace(span_type="RETRIEVER")
    def retrieve(self, question):
        q_emb = embed_texts([question], self.embedding_endpoint)[0]
        indices, scores = cosine_similarity_search(q_emb, self.embeddings, self.top_k)
        return [
            {
                "chunk_id": self.chunks[int(idx)]["chunk_id"],
                "text": self.chunks[int(idx)]["text"][:2000],
                "score": float(score),
            }
            for idx, score in zip(indices, scores)
        ]

    @mlflow.trace
    def query(self, question):
        retrieved = self.retrieve(question)
        context = "\n\n".join(f"[{r['chunk_id']}]\n{r['text']}" for r in retrieved)
        prompt = FLAT_RAG_SYSTEM_PROMPT.format(context=context)

        import mlflow.deployments
        client = mlflow.deployments.get_deploy_client("databricks")
        response = client.predict(
            endpoint=self.llm_endpoint,
            inputs={
                "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": question},
                ],
                "temperature": 0.1,
                "max_tokens": 2048,
            },
        )
        return response.choices[0]["message"]["content"]

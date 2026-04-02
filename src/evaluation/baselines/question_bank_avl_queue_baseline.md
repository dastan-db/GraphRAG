## AVL Queue Summary
corpus | review_targets | gap_targets | high_priority_reviews | high_severity_gaps | parallel_safe_targets
-------+----------------+-------------+-----------------------+--------------------+----------------------
bible  | 6              | 0           | 4                     | 0                  | 2                    
enron  | 6              | 0           | 6                     | 0                  | 2                    

## Gap Targets
(no rows)

## Review Targets
target_id                             | question_id                   | corpus | validation_status | review_priority | bucket_mismatch | latency_profile | question_text                                                                  
--------------------------------------+-------------------------------+--------+-------------------+-----------------+-----------------+-----------------+--------------------------------------------------------------------------------
review::bible-differential-a6a2315671 | bible-differential-a6a2315671 | bible  | needs_review      | high            | True            | policy_heavy    | How did Peter's role change across the biblical narrative?                     
review::bible-differential-c30ed57f46 | bible-differential-c30ed57f46 | bible  | needs_review      | high            | True            | policy_heavy    | What happened on the road to Damascus?                                         
review::bible-differential-ed395fb2fb | bible-differential-ed395fb2fb | bible  | needs_review      | high            | True            | policy_heavy    | What covenants are described in the biblical books?                            
review::bible-differential-fc707f963f | bible-differential-fc707f963f | bible  | needs_review      | high            | True            | policy_heavy    | How is David connected to both Ruth and Jesus?                                 
review::enron-abac-3c043d5ea2         | enron-abac-3c043d5ea2         | enron  | needs_review      | high            | True            | policy_heavy    | What happened between Enron and Arthur Andersen?                               
review::enron-abac-61104763af         | enron-abac-61104763af         | enron  | needs_review      | high            | True            | policy_heavy    | What legal issues were discussed in the email corpus?                          
review::enron-abac-ad927552d6         | enron-abac-ad927552d6         | enron  | needs_review      | high            | True            | policy_heavy    | Trace the connection between Kenneth Lay and Arthur Andersen.                  
review::enron-abac-c7db14aab8         | enron-abac-c7db14aab8         | enron  | needs_review      | high            | True            | policy_heavy    | What did Andrew Fastow discuss with legal counsel?                             
review::enron-core-36a6dceb6d         | enron-core-36a6dceb6d         | enron  | provisional       | high            | True            | search_heavy    | Find emails about the Arthur Andersen document destruction.                    
review::enron-core-8338f3fa30         | enron-core-8338f3fa30         | enron  | needs_review      | high            | True            | sql_only        | What percentage of Kenneth Lay's emails were from his assistant?               
review::bible-core-09c44e56ed         | bible-core-09c44e56ed         | bible  | provisional       | medium          | False           | judge_heavy     | What role does Jerusalem play across the biblical books in our knowledge graph?
review::bible-core-163fe24084         | bible-core-163fe24084         | bible  | provisional       | medium          | False           | mixed           | Who was Pharaoh and what was his role in the Exodus?                           
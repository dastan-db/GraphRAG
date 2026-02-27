from fastapi import FastAPI

from routers import auth, orders, payments

app = FastAPI(title="Order Service")

app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(orders.router, prefix="/orders", tags=["orders"])
app.include_router(payments.router, prefix="/payments", tags=["payments"])


@app.get("/health")
def health_check():
    return {"status": "ok"}

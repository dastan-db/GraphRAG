import uuid


class PaymentGateway:
    """Stub for an external payment processor (Stripe, etc.)."""

    def charge(self, amount: float, payment_method: str) -> dict:
        return {"id": str(uuid.uuid4()), "amount": amount, "status": "charged"}

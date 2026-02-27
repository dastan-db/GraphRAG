from fastapi import APIRouter, Depends

from services.auth_service import AuthService
from services.order_service import OrderService
from services.payment_service import PaymentService

router = APIRouter()
auth_service = AuthService()
order_service = OrderService()
payment_service = PaymentService()


@router.post("/process")
def process_payment(
    order_id: str,
    payment_method: str,
    current_user=Depends(auth_service.get_current_user),
):
    """Process payment for an order."""
    order = order_service.get_order(order_id)
    result = payment_service.process(order, payment_method)
    order_service.mark_paid(order_id)
    return result

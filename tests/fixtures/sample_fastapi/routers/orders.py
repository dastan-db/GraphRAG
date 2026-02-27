from fastapi import APIRouter, Depends

from services.auth_service import AuthService
from services.order_service import OrderService

router = APIRouter()
auth_service = AuthService()
order_service = OrderService()


@router.post("/")
def create_order(
    items: list[dict],
    current_user=Depends(auth_service.get_current_user),
):
    """Create a new order for the authenticated user."""
    return order_service.create_order(current_user["id"], items)


@router.get("/{order_id}")
def get_order(order_id: str, current_user=Depends(auth_service.get_current_user)):
    """Retrieve an order by its ID."""
    return order_service.get_order(order_id)

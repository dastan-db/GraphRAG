from fastapi import APIRouter, Depends

from services.auth_service import AuthService

router = APIRouter()
auth_service = AuthService()


@router.post("/login")
def login(username: str, password: str):
    """Authenticate a user and return an access token."""
    user = auth_service.authenticate(username, password)
    token = auth_service.generate_token(user)
    return {"access_token": token}


@router.get("/me")
def get_current_user_profile(current_user=Depends(auth_service.get_current_user)):
    """Return the profile of the currently authenticated user."""
    return current_user

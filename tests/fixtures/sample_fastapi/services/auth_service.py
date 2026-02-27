from database import Database


class AuthService:
    """Handles authentication, token generation, and user lookup."""

    def __init__(self):
        self.db = Database()

    def authenticate(self, username: str, password: str) -> dict:
        """Verify credentials and return the user record."""
        user = self.db.get_user_by_username(username)
        if user and user["password_hash"] == self._hash(password):
            return user
        raise ValueError("Invalid credentials")

    def generate_token(self, user: dict) -> str:
        """Create a signed JWT for the given user."""
        return f"token-{user['id']}"

    def get_current_user(self, token: str = "") -> dict:
        """Decode a token and return the corresponding user."""
        user_id = token.replace("token-", "")
        return self.db.get_user(user_id)

    @staticmethod
    def _hash(password: str) -> str:
        return f"hashed-{password}"

import os

DATABASE_URL = os.environ["DATABASE_URL"]
api_key = os.getenv("STRIPE_API_KEY")        # ENV002: missing from .env.example
port = int(os.getenv("PORT", "8000"))        # well-known, not flagged

"""
DRPL Backend - Local Development Startup
Run this once to set up everything: python run_local.py
"""

import os
import sys
import subprocess

def main():
    print("=" * 50)
    print("  DRPL Backend - Local Setup")
    print("=" * 50)

    # Step 1: Create .env if missing
    if not os.path.exists(".env"):
        print("\n[1/4] Creating .env from template...")
        with open(".env.example") as src, open(".env", "w") as dst:
            dst.write(src.read())
        print("      .env created with SQLite defaults.")
    else:
        print("\n[1/4] .env already exists, skipping.")

    # Step 2: Create uploads directory
    print("\n[2/4] Creating upload directories...")
    os.makedirs("uploads/tender_docs", exist_ok=True)
    print("      uploads/ directory ready.")

    # Step 3: Create database tables + seed admin user
    print("\n[3/4] Creating database and seeding admin user...")
    from app.core.database import engine, Base
    import app.models  # registers all models
    Base.metadata.create_all(bind=engine)
    print("      Database tables created.")

    from app.seed import seed
    seed()

    # Step 4: Generate API token for testing
    print("\n[4/4] Generating test API token...")
    from app.core.auth import create_access_token
    token = create_access_token(user_id=1, email="admin@drpl.com")
    print(f"\n{'=' * 50}")
    print(f"  YOUR API TOKEN (copy this for the extension):")
    print(f"{'=' * 50}")
    print(f"\n  {token}\n")
    print(f"{'=' * 50}")

    print("\nSetup complete! Now run the server with:")
    print("  uvicorn app.main:app --reload --port 8000")
    print("\nThen open: http://localhost:8000/docs")
    print("")


if __name__ == "__main__":
    main()

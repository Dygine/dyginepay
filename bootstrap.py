#!/usr/bin/env python3
"""
First-run setup. Creates the admin account and, optionally, a demo product.

Safe to run more than once: it never overwrites an existing admin, so a redeploy
that runs it again cannot reset your password.

    python bootstrap.py
    python bootstrap.py --demo      # also create a pgdesk product and plans
"""
from __future__ import annotations

import sys

from sqlalchemy import select

from app.core.config import settings
from app.core.crypto import encrypt, generate_secret, hash_password, hash_token
from app.core.database import session
from app.models import AdminUser, ApiKey, Plan, Product
from app.services import reference


def main() -> int:
    demo = "--demo" in sys.argv
    db = session()
    try:
        email = settings.ADMIN_EMAIL.strip().lower()
        existing = db.scalars(select(AdminUser).where(AdminUser.email == email)).first()

        if existing:
            print(f"Admin {email} already exists. Nothing changed.")
        else:
            if settings.is_production and settings.ADMIN_PASSWORD == "admin1234":
                print("Refusing to create a production admin with the default "
                      "password. Set ADMIN_PASSWORD first.")
                return 1
            db.add(AdminUser(email=email, name="Owner",
                             password_hash=hash_password(settings.ADMIN_PASSWORD)))
            db.commit()
            print(f"Created admin: {email}")
            if settings.ADMIN_PASSWORD == "admin1234":
                print("  Password is the default. Change ADMIN_PASSWORD now.")

        if demo:
            product = db.scalars(select(Product).where(Product.slug == "pgdesk")).first()
            if product is None:
                product = Product(
                    slug="pgdesk", name="PGDesk",
                    description="PG management platform",
                    webhook_secret_encrypted=encrypt(generate_secret(24)))
                db.add(product)
                db.flush()

                secret = generate_secret(32)
                key = ApiKey(product_id=product.id, mode="test",
                             key_id=reference.api_key_id("test"),
                             secret_hash=hash_token(secret),
                             secret_hint=secret[:6], label="bootstrap")
                db.add(key)

                for code, name, amount in [("starter", "PGDesk Starter", 49900),
                                           ("pro", "PGDesk Pro", 149900),
                                           ("scale", "PGDesk Scale", 399900)]:
                    db.add(Plan(product_id=product.id, code=code, name=name,
                                amount_paise=amount, interval="monthly",
                                sac=settings.DEFAULT_SAC))
                db.commit()

                print("\nDemo product created. These test credentials are shown "
                      "once:")
                print(f"  key id : {key.key_id}")
                print(f"  secret : {secret}")
            else:
                print("Demo product 'pgdesk' already exists.")

        print(f"\nSign in at {settings.BASE_URL.rstrip('/')}/admin")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

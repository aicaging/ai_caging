#!/usr/bin/env python3
"""Set password_hash for a user in a caging database, enabling UI login."""
import sys, os, bcrypt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from caging import database as db

if len(sys.argv) < 3:
    print("Usage: sudo python3 set_password.py <db_path> <username> [password]")
    sys.exit(1)

db_path = sys.argv[1]
username = sys.argv[2]
password = sys.argv[3] if len(sys.argv) > 3 else None

if not os.path.exists(db_path):
    print(f"✗ Database not found: {db_path}")
    sys.exit(1)

db.init_db(db_path)
user = db.get_user(username)
if not user:
    print(f"✗ User '{username}' not found in database")
    sys.exit(1)

if not password:
    password = input(f"Enter password for user '{username}': ")

pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
db.update_user(username, password_hash=pw_hash)

# Verify
updated = db.get_user(username)
has_pw = updated.get("password_hash") is not None
print(f"✓ password_hash set for '{username}': {'YES' if has_pw else 'FAILED'}")

# Test login via localhost
if has_pw:
    print(f"\nNow try: curl -X POST http://localhost:50080/ui/login \\")
    print(f"  -H 'Content-Type: application/json' \\")
    print(f"  -d '{{\"username\":\"{username}\",\"password\":\"{password}\"}}'")

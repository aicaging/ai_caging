#!/usr/bin/env python3
"""CLI tool for managing API keys in Caging."""
import argparse
import sys
import os

# Add parent dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from caging import database as db
from caging.auth import generate_api_key


def cmd_create(args):
    """Create a new client with API key."""
    db_path = args.database or os.environ.get("CAGING_DB", "caging.db")
    db.init_db(db_path)

    plain_key, hashed_key = generate_api_key()
    db.create_user(
        client_id=args.client_id,
        role=args.role,
        api_key_hash=hashed_key,
        system_user=args.system_user,
    )
    print(f"Client created: {args.client_id}")
    print(f"Role: {args.role}")
    print(f"System user: {args.system_user}")
    print(f"API Key: {plain_key}")
    print("Store this key securely - it will not be shown again.")


def cmd_list(args):
    """List all clients."""
    db_path = args.database or os.environ.get("CAGING_DB", "caging.db")
    db.init_db(db_path)

    users = db.list_users()
    if not users:
        print("No clients found.")
        return

    print(f"{'Client ID':<20} {'Role':<15} {'System User':<15} {'Created':<20}")
    print("-" * 70)
    for u in users:
        print(f"{u['id']:<20} {u['role']:<15} {u['system_user']:<15} {u.get('created_at', ''):<20}")


def cmd_delete(args):
    """Delete a client."""
    db_path = args.database or os.environ.get("CAGING_DB", "caging.db")
    db.init_db(db_path)

    if db.delete_user(args.client_id):
        print(f"Client deleted: {args.client_id}")
    else:
        print(f"Client not found: {args.client_id}")
        sys.exit(1)


def cmd_update(args):
    """Update a client."""
    db_path = args.database or os.environ.get("CAGING_DB", "caging.db")
    db.init_db(db_path)

    updates = {}
    if args.role:
        updates["role"] = args.role
    if args.system_user:
        updates["system_user"] = args.system_user

    if not updates:
        print("No updates specified.")
        return

    if db.update_user(args.client_id, **updates):
        print(f"Client updated: {args.client_id}")
    else:
        print(f"Client not found: {args.client_id}")
        sys.exit(1)


def cmd_reset_key(args):
    """Reset API key for a client."""
    db_path = args.database or os.environ.get("CAGING_DB", "caging.db")
    db.init_db(db_path)

    plain_key, hashed_key = generate_api_key()
    if db.update_user(args.client_id, api_key_hash=hashed_key):
        print(f"API key reset for: {args.client_id}")
        print(f"New API Key: {plain_key}")
    else:
        print(f"Client not found: {args.client_id}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Caging API Key Management")
    parser.add_argument("--database", "-d", help="Path to SQLite database (default: caging.db or $CAGING_DB)")

    sub = parser.add_subparsers(dest="command", required=True)

    # Create
    p_create = sub.add_parser("create", help="Create a new client")
    p_create.add_argument("--client-id", required=True, help="Client identifier")
    p_create.add_argument("--role", required=True, choices=["requester", "reviewer", "admin"],
                          help="Client role")
    p_create.add_argument("--system-user", required=True, help="Linux system user")

    # List
    sub.add_parser("list", help="List all clients")

    # Delete
    p_del = sub.add_parser("delete", help="Delete a client")
    p_del.add_argument("client_id", help="Client ID to delete")

    # Update
    p_upd = sub.add_parser("update", help="Update a client")
    p_upd.add_argument("client_id", help="Client ID to update")
    p_upd.add_argument("--role", choices=["requester", "reviewer", "admin"], help="New role")
    p_upd.add_argument("--system-user", help="New system user")

    # Reset key
    p_rst = sub.add_parser("reset-key", help="Reset API key for a client")
    p_rst.add_argument("client_id", help="Client ID to reset key for")

    args = parser.parse_args()

    commands = {
        "create": cmd_create,
        "list": cmd_list,
        "delete": cmd_delete,
        "update": cmd_update,
        "reset-key": cmd_reset_key,
    }

    commands[args.command](args)


if __name__ == "__main__":
    main()

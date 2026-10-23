"""Smoke test: log in to MicroStrategy, list projects, then log out.

Usage:  .venv\\Scripts\\python.exe scripts\\test_connection.py
"""
from mstrio.server import Environment

from mstr_connection import get_connection


def main() -> None:
    conn = get_connection()
    try:
        print(f"Connected to {conn.base_url} as {conn.username}")
        print(f"Server version: {conn.iserver_version} | Web version: {conn.web_version}")
        if conn.project_id:
            print(f"Active project ID: {conn.project_id}")

        projects = Environment(connection=conn).list_projects()
        print(f"\nProjects visible to this user ({len(projects)}):")
        for p in projects:
            print(f"  - {p.name}  ({p.id})")
    finally:
        conn.close()
        print("\nLogged out.")


if __name__ == "__main__":
    main()

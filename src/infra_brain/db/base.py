"""Shared SQLAlchemy declarative base.

Extracted into its own module so that both ``models.py`` and ``relationships.py``
can import ``Base`` without creating a circular import.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass

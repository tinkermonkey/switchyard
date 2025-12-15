---
name: database-expert
description: Expert in database design, migrations, query optimization, and ORM patterns. Use when designing database schemas, writing migrations, optimizing queries, or working with SQLAlchemy, Prisma, TypeORM. Supports PostgreSQL, MySQL, SQLite.
allowed-tools: Read, Grep, Glob, Bash(psql:*), Bash(mysql:*), Bash(sqlite3:*)
---

# Database Expert Skill

Expert in relational database design, migrations, and optimization.

## Expertise Areas

- **Schema Design**: Normalization, relationships, constraints
- **Migrations**: Version control for database changes
- **Query Optimization**: Indexing, query planning, performance
- **ORM Patterns**: SQLAlchemy, Prisma, Django ORM, TypeORM
- **Data Modeling**: Entity-relationship diagrams, data types
- **Database Operations**: Backups, replication, scaling

## Schema Design Principles

### Normalization

**1NF (First Normal Form):**
- Atomic values (no arrays in columns)
- Each column has single value type
- Unique rows (primary key)

**2NF (Second Normal Form):**
- 1NF + No partial dependencies
- All non-key columns depend on entire primary key

**3NF (Third Normal Form):**
- 2NF + No transitive dependencies
- Non-key columns depend only on primary key

### Relationship Types

**One-to-Many:**
```python
# SQLAlchemy
class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    posts = relationship('Post', back_populates='author')

class Post(Base):
    __tablename__ = 'posts'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False)
    author = relationship('User', back_populates='posts')
```

**Many-to-Many:**
```python
# Association table
user_roles = Table('user_roles', Base.metadata,
    Column('user_id', Integer, ForeignKey('users.id')),
    Column('role_id', Integer, ForeignKey('roles.id'))
)

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    roles = relationship('Role', secondary=user_roles, back_populates='users')

class Role(Base):
    __tablename__ = 'roles'
    id = Column(Integer, primary_key=True)
    users = relationship('User', secondary=user_roles, back_populates='roles')
```

## Migration Patterns

### Alembic (Python/SQLAlchemy)

```python
"""Add user preferences table

Revision ID: abc123
Revises: def456
Create Date: 2025-01-15
"""
from alembic import op
import sqlalchemy as sa

def upgrade():
    op.create_table(
        'user_preferences',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('theme', sa.String(20), default='light'),
        sa.Column('language', sa.String(10), default='en'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), onupdate=sa.func.now()),
    )

    # Add index
    op.create_index('ix_user_preferences_user_id', 'user_preferences', ['user_id'])

    # Add unique constraint
    op.create_unique_constraint('uq_user_preferences_user_id', 'user_preferences', ['user_id'])

def downgrade():
    op.drop_table('user_preferences')
```

### Prisma (TypeScript)

```prisma
// schema.prisma
model User {
  id         Int      @id @default(autoincrement())
  email      String   @unique
  name       String?
  posts      Post[]
  profile    Profile?
  createdAt  DateTime @default(now())
  updatedAt  DateTime @updatedAt

  @@index([email])
}

model Post {
  id        Int      @id @default(autoincrement())
  title     String
  content   String?
  published Boolean  @default(false)
  authorId  Int
  author    User     @relation(fields: [authorId], references: [id], onDelete: Cascade)
  createdAt DateTime @default(now())
  updatedAt DateTime @updatedAt

  @@index([authorId])
  @@index([published])
}

model Profile {
  id     Int    @id @default(autoincrement())
  bio    String?
  avatar String?
  userId Int    @unique
  user   User   @relation(fields: [userId], references: [id], onDelete: Cascade)
}
```

## Indexing Strategies

### When to Add Indexes

**Always index:**
- Primary keys (automatic)
- Foreign keys
- Columns in WHERE clauses
- Columns in JOIN conditions
- Columns in ORDER BY

**Consider indexing:**
- Frequently queried columns
- Columns with high cardinality
- Composite indexes for multi-column queries

**Avoid indexing:**
- Small tables (<1000 rows)
- Columns with low cardinality (boolean, few options)
- Frequently updated columns (index maintenance overhead)

### Index Types

**B-tree (default):**
```sql
CREATE INDEX idx_users_email ON users(email);
```

**Composite index:**
```sql
-- Order matters! Put most selective column first
CREATE INDEX idx_posts_author_status ON posts(author_id, status);
```

**Partial index:**
```sql
-- Index only active users
CREATE INDEX idx_users_active_email ON users(email) WHERE is_active = true;
```

**Full-text search (PostgreSQL):**
```sql
CREATE INDEX idx_posts_content_fulltext ON posts USING gin(to_tsvector('english', content));
```

## Query Optimization

### Analyze Queries

```sql
-- PostgreSQL
EXPLAIN ANALYZE
SELECT u.name, COUNT(p.id) as post_count
FROM users u
LEFT JOIN posts p ON u.id = p.author_id
WHERE u.is_active = true
GROUP BY u.id, u.name
ORDER BY post_count DESC
LIMIT 10;
```

### Common Optimizations

**1. Use indexes effectively:**
```python
# Bad: No index on email
User.query.filter(User.email == 'user@example.com').first()

# Good: Add index
# ALTER TABLE users ADD INDEX idx_users_email (email);
```

**2. Avoid N+1 queries:**
```python
# Bad: N+1 queries
users = User.query.all()
for user in users:
    print(user.posts)  # Separate query for each user

# Good: Eager loading
users = User.query.options(joinedload(User.posts)).all()
for user in users:
    print(user.posts)  # Already loaded
```

**3. Use pagination:**
```python
# Bad: Load all results
posts = Post.query.all()

# Good: Paginate
posts = Post.query.limit(20).offset(page * 20).all()
```

**4. Select only needed columns:**
```python
# Bad: SELECT *
users = User.query.all()

# Good: Select specific columns
users = db.session.query(User.id, User.name).all()
```

## Common Database Patterns

### Soft Deletes

```python
class SoftDeleteMixin:
    deleted_at = Column(DateTime, nullable=True)

    @classmethod
    def active(cls):
        return cls.query.filter(cls.deleted_at.is_(None))

class User(Base, SoftDeleteMixin):
    __tablename__ = 'users'
    # ... other columns

# Usage
User.active().all()  # Only non-deleted users
```

### Audit Trail

```python
class AuditMixin:
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, onupdate=func.now())
    created_by_id = Column(Integer, ForeignKey('users.id'))
    updated_by_id = Column(Integer, ForeignKey('users.id'))

class Document(Base, AuditMixin):
    __tablename__ = 'documents'
    # ... other columns
```

### Multi-Tenancy

**Shared Database with Tenant ID:**
```python
class TenantMixin:
    organization_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, index=True)

class User(Base, TenantMixin):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    # ... other columns

    __table_args__ = (
        UniqueConstraint('organization_id', 'email', name='uq_org_user_email'),
    )
```

## Data Types Best Practices

**Choose appropriate types:**
```python
# IDs: Integer or UUID
id = Column(Integer, primary_key=True)  # Auto-increment
id = Column(UUID, primary_key=True, default=uuid.uuid4)  # UUIDs

# Strings: Specify length
email = Column(String(255))  # Not Text for emails
description = Column(Text)  # For long text

# Timestamps: Use server defaults
created_at = Column(DateTime, server_default=func.now())
updated_at = Column(DateTime, onupdate=func.now())

# Booleans: NOT NULL with default
is_active = Column(Boolean, default=True, nullable=False)

# Decimals: For money
price = Column(Numeric(10, 2))  # 10 digits, 2 decimal places

# JSON: For flexible schemas
metadata = Column(JSON)  # PostgreSQL, MySQL 5.7+
```

## Transaction Patterns

```python
# SQLAlchemy
from sqlalchemy.orm import Session

def transfer_funds(session: Session, from_user: int, to_user: int, amount: float):
    """Transfer with transaction safety"""
    try:
        # Debit
        from_account = session.query(Account).filter_by(user_id=from_user).with_for_update().first()
        if from_account.balance < amount:
            raise ValueError("Insufficient funds")
        from_account.balance -= amount

        # Credit
        to_account = session.query(Account).filter_by(user_id=to_user).with_for_update().first()
        to_account.balance += amount

        session.commit()
    except Exception as e:
        session.rollback()
        raise
```

## Migration Checklist

**Before migration:**
- [ ] Backup database
- [ ] Test migration on staging
- [ ] Review migration for destructive changes
- [ ] Plan rollback strategy
- [ ] Check for dependent code changes

**Migration safety:**
- [ ] Use transactions where possible
- [ ] Add columns as nullable first
- [ ] Add indexes concurrently (PostgreSQL)
- [ ] Batch large data changes
- [ ] Monitor performance impact

**After migration:**
- [ ] Verify schema changes
- [ ] Run application tests
- [ ] Check query performance
- [ ] Monitor error logs
- [ ] Document changes

Use this skill for database schema design, migration planning, query optimization, and ORM best practices.

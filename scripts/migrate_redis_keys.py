#!/usr/bin/env python3
"""
Redis Key Migration Script: Docker Investigation Queues

Migrates Redis keys from old naming to new unified schema:
- Old: medic:investigation:*
- New: medic:docker_investigation:*

Keys affected:
- medic:investigation:queue → medic:docker_investigation:queue
- medic:investigation:active → medic:docker_investigation:active
- medic:investigation:status:{fingerprint_id} → medic:docker_investigation:status:{fingerprint_id}
- medic:investigation:pid:{fingerprint_id} → medic:docker_investigation:pid:{fingerprint_id}
- medic:investigation:result:{fingerprint_id} → medic:docker_investigation:result:{fingerprint_id}
- medic:investigation:started:{fingerprint_id} → medic:docker_investigation:started:{fingerprint_id}

Usage:
    python scripts/migrate_redis_keys.py --dry-run      # Preview changes
    python scripts/migrate_redis_keys.py --execute      # Run migration
    python scripts/migrate_redis_keys.py --verify       # Verify migration
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import List, Tuple, Dict

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

import redis

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class RedisKeyMigration:
    """Handles migration of Redis investigation keys to new schema."""

    OLD_KEY_PREFIX = "medic:investigation:"
    NEW_KEY_PREFIX = "medic:docker_investigation:"

    # Old key patterns
    OLD_QUEUE_KEY = "medic:investigation:queue"
    OLD_ACTIVE_SET_KEY = "medic:investigation:active"
    OLD_STATUS_PREFIX = "medic:investigation:status:"
    OLD_PID_PREFIX = "medic:investigation:pid:"
    OLD_RESULT_PREFIX = "medic:investigation:result:"
    OLD_STARTED_PREFIX = "medic:investigation:started:"

    # New key patterns
    NEW_QUEUE_KEY = "medic:docker_investigation:queue"
    NEW_ACTIVE_SET_KEY = "medic:docker_investigation:active"
    NEW_STATUS_PREFIX = "medic:docker_investigation:status:"
    NEW_PID_PREFIX = "medic:docker_investigation:pid:"
    NEW_RESULT_PREFIX = "medic:docker_investigation:result:"
    NEW_STARTED_PREFIX = "medic:docker_investigation:started:"

    def __init__(self, redis_host: str = "localhost", redis_port: int = 6379):
        """
        Initialize migration tool.

        Args:
            redis_host: Redis server host
            redis_port: Redis server port
        """
        self.redis = redis.Redis(
            host=redis_host,
            port=redis_port,
            decode_responses=True
        )
        logger.info(f"Connected to Redis: {redis_host}:{redis_port}")

    def find_old_keys(self) -> List[str]:
        """Find all old investigation keys."""
        pattern = f"{self.OLD_KEY_PREFIX}*"
        keys = []

        # Use SCAN instead of KEYS for production safety
        cursor = 0
        while True:
            cursor, partial_keys = self.redis.scan(cursor, match=pattern, count=100)
            keys.extend(partial_keys)
            if cursor == 0:
                break

        return sorted(keys)

    def generate_new_key(self, old_key: str) -> str:
        """
        Generate new key name from old key name.

        Args:
            old_key: Old key name

        Returns:
            New key name
        """
        return old_key.replace(self.OLD_KEY_PREFIX, self.NEW_KEY_PREFIX, 1)

    def get_key_type(self, key: str) -> str:
        """Get Redis data type of key."""
        return self.redis.type(key)

    def dry_run(self) -> Tuple[List[Tuple[str, str, str]], int]:
        """
        Preview migration without making changes.

        Returns:
            Tuple of (key_mappings, total_keys) where key_mappings is list of (old_key, new_key, type)
        """
        logger.info("=" * 80)
        logger.info("DRY RUN: Preview Redis Key Migration")
        logger.info("=" * 80)

        old_keys = self.find_old_keys()
        if not old_keys:
            logger.warning("No old keys found to migrate")
            return [], 0

        key_mappings = []
        for old_key in old_keys:
            new_key = self.generate_new_key(old_key)
            key_type = self.get_key_type(old_key)
            key_mappings.append((old_key, new_key, key_type))

            logger.info(f"  {old_key} ({key_type})")
            logger.info(f"    → {new_key}")

        logger.info("=" * 80)
        logger.info(f"Total keys to migrate: {len(key_mappings)}")
        logger.info("=" * 80)

        # Show key type breakdown
        type_counts: Dict[str, int] = {}
        for _, _, key_type in key_mappings:
            type_counts[key_type] = type_counts.get(key_type, 0) + 1

        logger.info("\nKey type breakdown:")
        for key_type, count in sorted(type_counts.items()):
            logger.info(f"  {key_type}: {count}")

        return key_mappings, len(key_mappings)

    def migrate_key(self, old_key: str, new_key: str) -> bool:
        """
        Migrate a single key to new name.

        Args:
            old_key: Source key name
            new_key: Destination key name

        Returns:
            True if successful, False otherwise
        """
        key_type = self.get_key_type(old_key)

        try:
            if key_type == 'string':
                # Copy string value
                value = self.redis.get(old_key)
                ttl = self.redis.ttl(old_key)
                self.redis.set(new_key, value)
                if ttl > 0:
                    self.redis.expire(new_key, ttl)

            elif key_type == 'list':
                # Copy list elements
                list_len = self.redis.llen(old_key)
                if list_len > 0:
                    elements = self.redis.lrange(old_key, 0, -1)
                    for element in elements:
                        self.redis.rpush(new_key, element)

            elif key_type == 'set':
                # Copy set members
                members = self.redis.smembers(old_key)
                if members:
                    self.redis.sadd(new_key, *members)

            elif key_type == 'zset':
                # Copy sorted set members with scores
                members = self.redis.zrange(old_key, 0, -1, withscores=True)
                if members:
                    self.redis.zadd(new_key, {member: score for member, score in members})

            elif key_type == 'hash':
                # Copy hash fields
                hash_data = self.redis.hgetall(old_key)
                if hash_data:
                    self.redis.hset(new_key, mapping=hash_data)

            else:
                logger.warning(f"Unknown key type '{key_type}' for {old_key}, skipping")
                return False

            return True

        except Exception as e:
            logger.error(f"Failed to migrate {old_key}: {e}")
            return False

    def execute_migration(self, delete_old_keys: bool = False) -> Tuple[int, int]:
        """
        Execute the migration.

        Args:
            delete_old_keys: If True, delete old keys after successful migration

        Returns:
            Tuple of (migrated_count, error_count)
        """
        logger.info("=" * 80)
        logger.info("EXECUTING REDIS KEY MIGRATION")
        logger.info("=" * 80)

        old_keys = self.find_old_keys()
        if not old_keys:
            logger.warning("No old keys found to migrate")
            return 0, 0

        migrated_count = 0
        error_count = 0

        for old_key in old_keys:
            new_key = self.generate_new_key(old_key)

            # Check if new key already exists
            if self.redis.exists(new_key):
                logger.warning(f"  Key {new_key} already exists, skipping...")
                continue

            logger.info(f"Migrating: {old_key} → {new_key}")

            if self.migrate_key(old_key, new_key):
                migrated_count += 1

                # Optionally delete old key
                if delete_old_keys:
                    self.redis.delete(old_key)
                    logger.info(f"  Deleted old key: {old_key}")
            else:
                error_count += 1

        logger.info("=" * 80)
        logger.info(f"Migration complete: {migrated_count} keys migrated, {error_count} errors")
        if not delete_old_keys:
            logger.info("Old keys preserved (not deleted)")
        logger.info("=" * 80)

        return migrated_count, error_count

    def verify_migration(self) -> bool:
        """
        Verify migration completed successfully.

        Returns:
            True if verification passed, False otherwise
        """
        logger.info("=" * 80)
        logger.info("VERIFYING REDIS KEY MIGRATION")
        logger.info("=" * 80)

        old_keys = self.find_old_keys()
        new_pattern = f"{self.NEW_KEY_PREFIX}*"

        # Find new keys
        new_keys = []
        cursor = 0
        while True:
            cursor, partial_keys = self.redis.scan(cursor, match=new_pattern, count=100)
            new_keys.extend(partial_keys)
            if cursor == 0:
                break

        logger.info(f"Old keys ({self.OLD_KEY_PREFIX}*): {len(old_keys)}")
        logger.info(f"New keys ({self.NEW_KEY_PREFIX}*): {len(new_keys)}")

        if len(old_keys) == len(new_keys):
            logger.info("✅ Key counts match!")
            success = True
        else:
            logger.warning(f"⚠️  Key count mismatch: {len(old_keys)} vs {len(new_keys)}")
            success = False

        # Verify specific key types
        if new_keys:
            logger.info("\nVerifying key types:")

            # Check queue key
            if self.redis.exists(self.NEW_QUEUE_KEY):
                queue_type = self.get_key_type(self.NEW_QUEUE_KEY)
                queue_len = self.redis.llen(self.NEW_QUEUE_KEY)
                logger.info(f"  {self.NEW_QUEUE_KEY}: {queue_type}, {queue_len} items")

            # Check active set key
            if self.redis.exists(self.NEW_ACTIVE_SET_KEY):
                active_type = self.get_key_type(self.NEW_ACTIVE_SET_KEY)
                active_count = self.redis.scard(self.NEW_ACTIVE_SET_KEY)
                logger.info(f"  {self.NEW_ACTIVE_SET_KEY}: {active_type}, {active_count} members")

            # Check status keys
            status_keys = [k for k in new_keys if k.startswith(self.NEW_STATUS_PREFIX)]
            if status_keys:
                logger.info(f"  Status keys: {len(status_keys)}")
                # Sample verification
                sample_key = status_keys[0]
                sample_value = self.redis.get(sample_key)
                logger.info(f"    Sample: {sample_key} = {sample_value}")

        logger.info("=" * 80)
        return success


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Migrate Redis investigation keys to new schema"
    )
    parser.add_argument(
        '--mode',
        choices=['dry-run', 'execute', 'verify'],
        default='dry-run',
        help='Migration mode (default: dry-run)'
    )
    parser.add_argument(
        '--redis-host',
        default='localhost',
        help='Redis server host (default: localhost)'
    )
    parser.add_argument(
        '--redis-port',
        type=int,
        default=6379,
        help='Redis server port (default: 6379)'
    )
    parser.add_argument(
        '--delete-old-keys',
        action='store_true',
        help='Delete old keys after successful migration'
    )

    # Legacy flags for backward compatibility
    parser.add_argument('--dry-run', action='store_true', help='Run in dry-run mode')
    parser.add_argument('--execute', action='store_true', help='Execute migration')
    parser.add_argument('--verify', action='store_true', help='Verify migration')

    args = parser.parse_args()

    # Handle legacy flags
    if args.dry_run:
        args.mode = 'dry-run'
    elif args.execute:
        args.mode = 'execute'
    elif args.verify:
        args.mode = 'verify'

    # Initialize migrator
    migrator = RedisKeyMigration(
        redis_host=args.redis_host,
        redis_port=args.redis_port
    )

    # Execute based on mode
    if args.mode == 'dry-run':
        key_mappings, total = migrator.dry_run()
        logger.info("\nTo execute migration, run:")
        logger.info("  python scripts/migrate_redis_keys.py --execute")
        logger.info("\nTo execute migration and delete old keys:")
        logger.info("  python scripts/migrate_redis_keys.py --execute --delete-old-keys")

    elif args.mode == 'execute':
        # Find keys first
        old_keys = migrator.find_old_keys()

        if not old_keys:
            logger.info("No keys to migrate")
            return

        logger.info(f"\n⚠️  About to migrate {len(old_keys)} Redis keys")
        if args.delete_old_keys:
            logger.info("Old keys will be DELETED after migration")
        else:
            logger.info("Old keys will be preserved for backup")
        logger.info("")

        response = input("Continue? [y/N]: ")
        if response.lower() != 'y':
            logger.info("Migration cancelled")
            return

        migrated, errors = migrator.execute_migration(delete_old_keys=args.delete_old_keys)

        logger.info("\nTo verify migration, run:")
        logger.info("  python scripts/migrate_redis_keys.py --verify")

    elif args.mode == 'verify':
        success = migrator.verify_migration()
        if success:
            logger.info("\n✅ Migration verification passed!")
            logger.info("\nOld keys are preserved unless --delete-old-keys was used.")
        else:
            logger.warning("\n⚠️  Migration verification failed!")
            logger.warning("Review the errors above and re-run migration if needed.")
            sys.exit(1)


if __name__ == "__main__":
    main()

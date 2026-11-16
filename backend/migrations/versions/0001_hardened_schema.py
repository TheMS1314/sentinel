"""Adopt legacy databases and migrate without resetting recorded requests."""
from alembic import op
import sqlalchemy as sa
revision = '0001_hardened'
down_revision = None
branch_labels = depends_on = None


def upgrade():
    from app.models.db import Base
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if 'transactions' not in tables:
        Base.metadata.create_all(bind)
        return
    # Fail before changes if legacy policy history cannot be made unambiguous.
    duplicate = bind.execute(sa.text('SELECT agent_id, version FROM policies GROUP BY agent_id, version HAVING COUNT(*) > 1')).first()
    if duplicate:
        raise RuntimeError('Duplicate policy versions: resolve explicitly before migration; no history was deleted')
    existing = {c['name'] for c in inspector.get_columns('decisions')}
    additions = [c for c in Base.metadata.tables['decisions'].columns if c.name not in existing]
    for column in additions:
        op.add_column('decisions', sa.Column(column.name, column.type, nullable=True))
    if bind.dialect.name == 'postgresql':
        op.alter_column('transactions', 'amount', type_=sa.Numeric(14, 2), postgresql_using='round(amount::numeric, 2)')
    # Keep the latest active version only, preserving every older policy row.
    bind.execute(sa.text('''UPDATE policies SET active = false WHERE active = true AND id NOT IN
+        (SELECT MAX(id) FROM policies WHERE active = true GROUP BY agent_id)'''.replace('+', '')))
    indexes = {i['name'] for i in sa.inspect(bind).get_indexes('policies')}
    if 'uq_policy_active' not in indexes:
        op.create_index('uq_policy_active', 'policies', ['agent_id'], unique=True,
                        postgresql_where=sa.text('active'), sqlite_where=sa.text('active'))
    constraints = {i['name'] for i in sa.inspect(bind).get_unique_constraints('policies')}
    if 'uq_policy_agent_version' not in constraints:
        with op.batch_alter_table('policies') as batch:
            batch.create_unique_constraint('uq_policy_agent_version', ['agent_id', 'version'])
    Base.metadata.create_all(bind)
    for index in Base.metadata.tables['transactions'].indexes:
        index.create(bind, checkfirst=True)
    # Legacy approvals are conservatively reserved as UNKNOWN. Migration does
    # not call the gateway or assume they were captured. Operators reconcile them.
    bind.execute(sa.text('''INSERT INTO payments (request_id, state, amount_paise)
        SELECT t.request_id, 'unknown', CAST(round(t.amount * 100) AS BIGINT)
        FROM transactions t JOIN decisions d ON d.request_id=t.request_id
        WHERE (d.verdict='APPROVE' OR d.human_override='APPROVE')
        AND NOT EXISTS (SELECT 1 FROM payments p WHERE p.request_id=t.request_id)'''))


def downgrade():
    raise RuntimeError('Destructive downgrade disabled; restore a reviewed backup instead')

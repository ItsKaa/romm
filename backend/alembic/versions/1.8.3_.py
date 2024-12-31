"""update to 1.8.3

Revision ID: 1.8.3
Revises: 1.8.2
Create Date: 2023-05-17 12:59:44.344356

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "1.8.3"
down_revision = "1.8.2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    with op.batch_alter_table("roms") as batch_op:
        batch_op.execute(
            "UPDATE roms SET file_path = REPLACE(file_path, '/romm/library/', '')"
        )
        batch_op.execute(
            "UPDATE roms SET path_cover_s = REPLACE(path_cover_s, '/romm/resources/', '')"
        )
        batch_op.execute(
            "UPDATE roms SET path_cover_l = REPLACE(path_cover_l, '/romm/resources/', '')"
        )
        if connection.engine.name == "postgresql":
            batch_op.execute(
                "UPDATE roms SET path_screenshots = REPLACE(path_screenshots::text, '/romm/resources/', '')::jsonb"
            )
        else:
            batch_op.execute(
                "UPDATE roms SET path_screenshots = REPLACE(path_screenshots, '/romm/resources/', '')"
            )


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    pass
    # ### end Alembic commands ###

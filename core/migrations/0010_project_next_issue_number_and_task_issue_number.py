from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0009_migrate_task_status_and_reporter"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="next_issue_number",
            field=models.PositiveIntegerField(default=1),
        ),
        migrations.AddField(
            model_name="task",
            name="issue_number",
            field=models.PositiveIntegerField(blank=True, db_index=True, null=True),
        ),
    ]


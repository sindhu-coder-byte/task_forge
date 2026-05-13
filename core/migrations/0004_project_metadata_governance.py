# Generated migration for new Project fields

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0003_alter_profile_role'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # ====== METADATA & CATEGORIZATION ======
        migrations.AddField(
            model_name='project',
            name='category',
            field=models.CharField(
                blank=True,
                choices=[
                    ('engineering', 'Engineering'),
                    ('marketing', 'Marketing'),
                    ('sales', 'Sales'),
                    ('product', 'Product'),
                    ('design', 'Design'),
                    ('operations', 'Operations'),
                    ('hr', 'HR'),
                    ('finance', 'Finance'),
                    ('other', 'Other'),
                ],
                default='other',
                max_length=50,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name='project',
            name='project_url',
            field=models.URLField(
                blank=True,
                help_text='Link to documentation, repo, or external resource',
                max_length=500,
                null=True,
            ),
        ),

        # ====== VISUAL IDENTITY ======
        migrations.AddField(
            model_name='project',
            name='avatar',
            field=models.ImageField(
                blank=True,
                help_text='Project icon or logo',
                null=True,
                upload_to='project_avatars/',
            ),
        ),

        # ====== TIMELINE & PRIORITY ======
        migrations.AddField(
            model_name='project',
            name='start_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='project',
            name='target_end_date',
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='project',
            name='priority',
            field=models.CharField(
                blank=True,
                choices=[
                    ('p1', 'P1 - Critical'),
                    ('p2', 'P2 - High'),
                    ('p3', 'P3 - Medium'),
                    ('p4', 'P4 - Low'),
                ],
                default='p3',
                max_length=10,
                null=True,
            ),
        ),

        # ====== SECURITY & GOVERNANCE ======
        migrations.AddField(
            model_name='project',
            name='is_private',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='project',
            name='default_assignee',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='default_projects',
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]

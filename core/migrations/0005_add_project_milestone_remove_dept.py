import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0004_simplify_department_role'),
    ]

    operations = [
        # Delete child table first to avoid FK constraint errors
        migrations.DeleteModel(name='DepartmentRole'),
        migrations.DeleteModel(name='Department'),
        migrations.CreateModel(
            name='ProjectMilestone',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('icon', models.CharField(
                    choices=[
                        ('ri-flag-line',         'Flag — Initiated'),
                        ('ri-palette-line',      'Palette — Design/UI-UX'),
                        ('ri-code-s-slash-line', 'Code — Development'),
                        ('ri-bug-line',          'Bug — QA/Testing'),
                        ('ri-rocket-line',       'Rocket — Deployment'),
                        ('ri-truck-line',        'Truck — Delivery'),
                        ('ri-user-line',         'User — Business/Client'),
                        ('ri-database-line',     'Database — Data/Backend'),
                        ('ri-shield-check-line', 'Shield — Security'),
                        ('ri-bar-chart-line',    'Chart — Analytics'),
                        ('ri-settings-line',     'Settings — DevOps'),
                        ('ri-team-line',         'Team — Collaboration'),
                    ],
                    default='ri-flag-line', max_length=60,
                )),
                ('color', models.CharField(default='#4f46e5', help_text='Hex colour e.g. #4f46e5', max_length=20)),
                ('role_key', models.CharField(blank=True, max_length=50)),
                ('order', models.PositiveSmallIntegerField(default=0)),
                ('is_active', models.BooleanField(default=True)),
                ('project', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='milestones', to='core.project',
                )),
            ],
            options={'ordering': ['order']},
        ),
    ]

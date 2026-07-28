from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from config import settings as app_settings

from .models import Profile, Project, ProjectMembership


class ProjectUserSelectionTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username='admin', email='admin@example.com', password='pw12345')
        Profile.objects.create(user=self.admin, role='admin')

        self.project = Project.objects.create(
            name='Alpha Project',
            description='Test project',
            created_by=self.admin,
            project_lead=self.admin,
            department='engineering',
        )
        ProjectMembership.objects.update_or_create(
            user=self.admin,
            project=self.project,
            defaults={'role': 'project_lead'},
        )

        self.manual_user = User.objects.create_user(
            username='manual-user',
            email='manual@example.com',
            password='pw12345',
        )
        Profile.objects.create(user=self.manual_user, role='user')

    def test_create_task_page_lists_active_manual_users_for_selected_project(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse('core:create_task'), {'project': self.project.id})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'manual-user')

    def test_project_detail_page_lists_active_manual_users_for_team_members(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse('core:project_detail', args=[self.project.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'manual-user')

    def test_missing_user_delete_redirects_to_user_list(self):
        self.client.force_login(self.admin)

        response = self.client.post(reverse('core:user_delete', args=[999999]), follow=True)

        self.assertRedirects(response, reverse('core:user_list'))
        self.assertContains(response, 'User not found.')


class DatabaseConfigTests(TestCase):
    def test_local_debug_mysql_urls_fallback_to_sqlite(self):
        self.assertTrue(app_settings._should_use_sqlite_fallback('mysql://example.com/db', debug=True))
        self.assertFalse(app_settings._should_use_sqlite_fallback('mysql://example.com/db', debug=True, force_mysql=True))

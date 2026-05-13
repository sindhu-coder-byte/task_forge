from django import forms
from django.contrib.auth.models import User
from django.db import transaction
from .models import Project, Profile, ProjectMembership


# ============================================================
# USER CREATE FORM
# ============================================================

class UserCreateForm(forms.ModelForm):

    USER_TYPE_CHOICES = [
        ('regular', 'Manual User'),
        ('google', 'Google User'),
    ]

    user_type = forms.ChoiceField(
        choices=USER_TYPE_CHOICES,
        initial='regular',
        widget=forms.RadioSelect(attrs={'class': 'user-type-radio'})
    )

    username = forms.CharField(
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Enter username'})
    )

    password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Enter password'})
    )

    is_active = forms.BooleanField(required=False, initial=True)

    class Meta:
        model = User
        fields = ['username', 'email', 'password', 'is_active']

    def clean(self):
        cleaned_data = super().clean()
        user_type = cleaned_data.get('user_type')
        username = cleaned_data.get('username')
        password = cleaned_data.get('password')
        email = cleaned_data.get('email')

        if not email:
            raise forms.ValidationError('Email is required')

        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError('A user with this email already exists.')

        if user_type == 'google':
            cleaned_data['password'] = None
            cleaned_data['username'] = username or ''
        else:
            if not username:
                raise forms.ValidationError('Username is required for manual users')
            if User.objects.filter(username__iexact=username).exists():
                raise forms.ValidationError('Username already exists')
            if not password:
                raise forms.ValidationError('Password is required for manual users')

        return cleaned_data

    @transaction.atomic
    def save(self, commit=True):
        user = super().save(commit=False)
        user_type = self.cleaned_data.get('user_type')
        email = self.cleaned_data.get('email')

        if user_type == 'google':
            base = email.split('@')[0]
            username = base
            counter = 1
            while User.objects.filter(username=username).exists():
                username = f"{base}{counter}"
                counter += 1
            user.username = username
            user.set_unusable_password()
        else:
            user.set_password(self.cleaned_data['password'])

        user.is_active = self.cleaned_data.get('is_active', True)

        if commit:
            user.save()
            profile, _ = Profile.objects.get_or_create(user=user)
            profile.role = 'user'
            if user_type == 'google':
                profile.oauth_provider = 'google'
            profile.save()

        return user


# ============================================================
# USER UPDATE FORM
# ============================================================

class UserUpdateForm(forms.ModelForm):
    is_active = forms.BooleanField(required=False)

    assigned_projects = forms.ModelMultipleChoiceField(
        queryset=Project.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={'class': 'form-control', 'size': '6'}),
    )

    class Meta:
        model = User
        fields = ["username", "email", "is_active"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance:
            self.fields["is_active"].initial = self.instance.is_active
            self.fields["assigned_projects"].initial = Project.objects.filter(
                projectmembership__user=self.instance
            )

    def clean_username(self):
        username = self.cleaned_data.get("username")
        qs = User.objects.filter(username=username).exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("Username already exists")
        return username

    def clean_email(self):
        email = self.cleaned_data.get("email")
        qs = User.objects.filter(email=email).exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("Email already exists")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_active = self.cleaned_data.get("is_active", False)

        if commit:
            user.save()

            selected_projects = self.cleaned_data.get("assigned_projects") or []

            if selected_projects:
                ProjectMembership.objects.filter(user=user).exclude(
                    project__in=selected_projects
                ).delete()

            for project in selected_projects:
                ProjectMembership.objects.get_or_create(
                    user=user,
                    project=project,
                    defaults={"role": "developer"}
                )

        return user


# ============================================================
# MEMBERSHIP FORM
# ============================================================

class MembershipForm(forms.Form):
    project = forms.ModelChoiceField(
        queryset=Project.objects.all(),
        widget=forms.Select(attrs={'class': 'form-control'}),
        label='Project'
    )
    role = forms.ChoiceField(
        choices=ProjectMembership.ROLE_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'}),
        label='Role'
    )
    
    
# ============================================================
# TEAM CREATE FORM
# ============================================================

from .models import (
    Team,
    Notification,
)

from django import forms
from django.contrib.auth.models import User
from django.db import transaction

from .models import Team, Project, Notification


class TeamCreateForm(forms.ModelForm):

    lead = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
        widget=forms.Select(attrs={
            'class': 'team-input'
        })
    )

    members = forms.ModelMultipleChoiceField(
        queryset=User.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple
    )

    class Meta:
        model = Team

        fields = [
            'name',
            'description',
            'team_type',
            'capacity',
            'lead',
            'members',
            'color',
        ]

        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'team-input',
                'placeholder': 'Backend Squad'
            }),

            'description': forms.Textarea(attrs={
                'class': 'team-input',
                'rows': 3,
                'placeholder': 'Describe team responsibilities'
            }),

            'team_type': forms.Select(attrs={
                'class': 'team-input'
            }),

            'capacity': forms.NumberInput(attrs={
                'class': 'team-input',
                'min': 1,
                'max': 100
            }),

            'color': forms.TextInput(attrs={
                'type': 'color',
                'class': 'team-color-input'
            }),
        }

    def __init__(self, *args, **kwargs):

        self.project = kwargs.pop('project', None)
        self.request_user = kwargs.pop('request_user', None)

        super().__init__(*args, **kwargs)

        if self.project:

            department_users = User.objects.filter(
                projects__department=self.project.department
            ).distinct().select_related('profile')

            self.fields['lead'].queryset = department_users
            self.fields['members'].queryset = department_users

    def clean_name(self):

        name = self.cleaned_data['name'].strip()

        if Team.objects.filter(
            project=self.project,
            name__iexact=name
        ).exists():

            raise forms.ValidationError(
                "Team name already exists."
            )

        return name

    def clean(self):

        cleaned = super().clean()

        lead = cleaned.get('lead')
        members = cleaned.get('members')

        if lead and members and lead in members:
            members = members.exclude(id=lead.id)
            cleaned['members'] = members

        capacity = cleaned.get('capacity')

        if members and capacity:
            if members.count() > capacity:
                raise forms.ValidationError(
                    "Members exceed team capacity."
                )

        return cleaned

    @transaction.atomic
    def save(self, commit=True):

        team = super().save(commit=False)

        team.project = self.project
        team.created_by = self.request_user

        if commit:
            team.save()

            members = self.cleaned_data.get('members')
            lead = self.cleaned_data.get('lead')

            if members:
                team.members.set(members)

            if lead:
                team.lead = lead
                team.save()

            # =========================
            # PROJECT MEMBERSHIP
            # =========================

            all_users = []

            if members:
                all_users.extend(list(members))

            if lead:
                all_users.append(lead)

            for user in all_users:

                ProjectMembership.objects.get_or_create(
                    user=user,
                    project=self.project,
                    defaults={
                        'role': 'developer'
                    }
                )

            # =========================
            # NOTIFICATIONS
            # =========================

            notifications = []

            for user in all_users:

                notifications.append(
                    Notification(
                        user=user,
                        notification_type='member_added',
                        title='Added To Team',
                        message=(
                            f'You were added to '
                            f'"{team.name}" team.'
                        ),
                        project=self.project
                    )
                )

            Notification.objects.bulk_create(notifications)

        return team
    
    
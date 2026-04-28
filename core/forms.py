from django import forms
from django.contrib.auth.models import User
from .models import Project, Profile, ProjectMembership


# ============================================================
# USER CREATE FORM
# ============================================================
class UserCreateForm(forms.ModelForm):

    assigned_project = forms.ModelChoiceField(
        queryset=Project.objects.all(),
        required=True
    )

    role = forms.ChoiceField(
        choices=ProjectMembership.ROLE_CHOICES,
        widget=forms.Select(attrs={'class': 'form-control'})
    )
    
    

    is_active = forms.BooleanField(required=False, initial=True)

    class Meta:
        model = User
        fields = ['username', 'email', 'password', 'is_active']
        widgets = {
            'username': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Enter username'
            }),
            'email': forms.EmailInput(attrs={
                'class': 'form-control',
                'placeholder': 'Enter email'
            }),
            
            'password': forms.PasswordInput(attrs={
             'class': 'form-control',
             'placeholder': 'Enter password'
            }),
        }

    # ---------------- VALIDATION ---------------- #
    def clean_username(self):
        username = self.cleaned_data.get("username")
        if User.objects.filter(username=username).exists():
            raise forms.ValidationError("Username already exists")
        return username

    def clean_email(self):
        email = self.cleaned_data.get("email")
        if User.objects.filter(email=email).exists():
            raise forms.ValidationError("Email already exists")
        return email

    # ---------------- SAVE LOGIC ---------------- #
    def save(self, commit=True):
      user = super().save(commit=False)
      user.set_password(self.cleaned_data["password"])

      if commit:
        user.save()

        project = self.cleaned_data["assigned_project"]
        role = self.cleaned_data["role"]

        ProjectMembership.objects.create(
            user=user,
            project=project,
            role=role
        )

      return user


# ============================================================
# USER UPDATE FORM
# ============================================================

class UserUpdateForm(forms.ModelForm):
    is_active = forms.BooleanField(required=False)

    # Select projects
    assigned_projects = forms.ModelMultipleChoiceField(
        queryset=Project.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={'class': 'form-control', 'size': '6'}),
    )

    class Meta:
        model = User
        fields = ["username", "email", "is_active"]

    # ---------------- INIT ---------------- #
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance:
            self.fields["is_active"].initial = self.instance.is_active

            # ✅ Load assigned projects
            self.fields["assigned_projects"].initial = Project.objects.filter(
                projectmembership__user=self.instance
            )

    # ---------------- VALIDATION ---------------- #
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

    # ---------------- SAVE ---------------- #
    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_active = self.cleaned_data.get("is_active", False)

        if commit:
            user.save()

            selected_projects = self.cleaned_data.get("assigned_projects") or []

            # ✅ Remove old memberships (only if projects were explicitly selected)
            if selected_projects:
                ProjectMembership.objects.filter(user=user).exclude(
                    project__in=selected_projects
                ).delete()

            # ✅ Add missing memberships (default role)
            for project in selected_projects:
                ProjectMembership.objects.get_or_create(
                    user=user,
                    project=project,
                    defaults={"role": "developer"}  # default role
                )

        return user
    
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
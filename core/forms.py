from django import forms
from django.contrib.auth.models import User
from .models import Project, Profile


# ============================================================
# USER CREATE FORM
# ============================================================
class UserCreateForm(forms.ModelForm):
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control pw-input',
            'placeholder': 'Enter password'
        })
    )

    role = forms.ChoiceField(
        choices=Profile.ROLE_CHOICES,
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
        user.is_active = self.cleaned_data.get("is_active", True)

        if commit:
            user.save()

            # ✅ Create Profile safely
            Profile.objects.update_or_create(
                user=user,
                defaults={"role": self.cleaned_data["role"]}
            )

        return user


# ============================================================
# USER UPDATE FORM
# ============================================================
class UserUpdateForm(forms.ModelForm):
    role = forms.ChoiceField(
        choices=Profile.ROLE_CHOICES,  # ✅ No hardcoding
        widget=forms.Select(attrs={'class': 'form-control'})
    )

    is_active = forms.BooleanField(required=False)
    
    # Add project assignment for admins
    assigned_projects = forms.ModelMultipleChoiceField(
        queryset=Project.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={'class': 'form-control', 'size': '6'}),
        help_text="Select projects to assign this user to"
    )

    class Meta:
        model = User
        fields = ["username", "email", "is_active"]
        widgets = {
            'username': forms.TextInput(attrs={'class': 'form-control'}),
            'email': forms.EmailInput(attrs={'class': 'form-control'}),
        }

    # ---------------- INIT ---------------- #
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if self.instance:
            self.fields["is_active"].initial = self.instance.is_active

            if hasattr(self.instance, 'profile'):
                self.fields["role"].initial = self.instance.profile.role
            
            # Set initial assigned projects
            self.fields["assigned_projects"].initial = self.instance.project_members.all()

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

    # ---------------- SAVE LOGIC ---------------- #
    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_active = self.cleaned_data.get("is_active", False)

        if commit:
            user.save()

            # ✅ Sync Profile role
            Profile.objects.update_or_create(
                user=user,
                defaults={"role": self.cleaned_data["role"]}
            )

        return user
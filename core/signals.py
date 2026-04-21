from allauth.socialaccount.signals import pre_social_login
from django.dispatch import receiver
from django.contrib.auth.models import User
from core.models import Profile

@receiver(pre_social_login)
def handle_google_login(request, sociallogin, **kwargs):
    email = sociallogin.account.extra_data.get('email')

    if not email:
        return

    user = User.objects.filter(email__iexact=email).first()

    # ✅ If user already exists → normal flow
    if user:
        sociallogin.connect(request, user)
        return

    # ✅ NEW: Allow random Google login → create user
    username = email.split("@")[0]

    user = User.objects.create_user(
        username=username,
        email=email
    )
    user.set_unusable_password()
    user.save()

    # 🔒 Assign SAFE default role
    profile, _ = Profile.objects.get_or_create(user=user)
    profile.role = "guest"   # VERY IMPORTANT
    profile.save()

    sociallogin.connect(request, user)
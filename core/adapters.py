from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.contrib.auth.models import User
from .models import Profile, ProjectInvite

class CustomSocialAccountAdapter(DefaultSocialAccountAdapter):

    def pre_social_login(self, request, sociallogin):
        email = sociallogin.account.extra_data.get('email')

        if not email:
            return

        existing_user = User.objects.filter(email__iexact=email).first()

        if existing_user:
            # ✅ EXISTING USER
            sociallogin.connect(request, existing_user)

        else:
            # ✅ RANDOM GOOGLE LOGIN → ALLOWED
            username = email.split("@")[0]

            new_user = User.objects.create_user(
                username=username,
                email=email
            )
            new_user.set_unusable_password()
            new_user.save()

            # ✅ DEFAULT PROFILE
            profile, _ = Profile.objects.get_or_create(user=new_user)
            profile.role = 'user'
            profile.save()

            sociallogin.connect(request, new_user)

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)

        # ✅ APPLY INVITE AFTER GOOGLE LOGIN
        invite = ProjectInvite.objects.filter(
            email__iexact=user.email,
            used=False
        ).first()

        if invite:
            invite.project.members.add(user)

            profile, _ = Profile.objects.get_or_create(user=user)

            if profile.role == 'user':
                profile.role = invite.role
                profile.save()

            invite.used = True
            invite.save()

        return user
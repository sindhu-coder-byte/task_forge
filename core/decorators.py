from django.shortcuts import redirect

def role_required(allowed_roles=[]):
    def decorator(view_func):
        def wrapper(request, *args, **kwargs):
            profile = getattr(request.user, 'profile', None)

            if profile and profile.role in allowed_roles:
                return view_func(request, *args, **kwargs)

            return redirect('core:home')
        return wrapper
    return decorator
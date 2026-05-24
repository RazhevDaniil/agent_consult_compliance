from fastapi import HTTPException


class PermissionDeniedException(HTTPException):
    def __init__(self, *args):
        self.code = 403
        if args:
            self.message = args[0]
        else:
            self.message = None

    def __str__(self):
        if self.message:
            return f'PermissionDenied, {self.message}.'
        else:
            return 'You do not have access to this resource.'

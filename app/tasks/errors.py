class TaskAssistantError(RuntimeError):
    pass


class ConfirmationRequired(TaskAssistantError):
    pass


class DatabaseCorruptError(TaskAssistantError):
    pass

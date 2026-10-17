"""Frozen execution candidates evaluated independently from the planner."""
def task_complexity(task):
    levels = ('low', 'medium', 'high')
    rank = max(levels.index(task.complexity), levels.index(task.risk))
    if len(task.expected_paths) > 4:
        rank = max(rank, 1)
    if len(task.expected_paths) > 8:
        rank = 2
    return levels[rank]


class TaskRouter:
    def __init__(self, routes, *, local_ready):
        self.routes, self.local_ready = routes, local_ready

    def select(self, task, strategy, local_safe):
        complexity = task_complexity(task)
        candidates = self.routes[complexity]
        eligible = [r for r in candidates if
            (r.provider == 'ollama' and self.local_ready and local_safe and strategy != 'FRONTIER_ONLY') or
            (r.provider != 'ollama' and strategy != 'LOCAL_OFFLINE' and
             (strategy != 'FRONTIER_ONLY' or r.cost_class == 'subscription'))]
        if not eligible:
            raise ValueError(f'No allowed execution model is suitable for task {task.key} ({complexity}). '
                'Allow/setup suitable providers in Project access, or revise the task. The planner is not an implicit executor.')
        return eligible[0], next((r for r in eligible[1:] if r.provider != 'ollama'), None), complexity

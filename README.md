# Syllabus

Syllabus is an API for designing curricula for reinforcement learning agents, as well as a framework for synchronizing those curricula across environments running in multiple processes. It currently has support for environments run with Python native multiprocessing or Ray actors, which includes RL libraries such as RLLib, CleanRL, Stable Baselines 3, and Monobeast (Torchbeast). We currently have working examples with **CleanRL**, **RLLib**, and **Monobeast (Torchbeast)**. We also have preliminary support and examples for multiagent **PettingZoo** environments.

WIP Documentation is available at https://ryannavillus.github.io/Syllabus/index.html


## How it works

Syllabus uses a bidirectional sender-receiver model in which the curriculum sends tasks and receives environment outputs, while the environment receives tasks and sends outputs. The environment can run the provided task in the next episode and the curriculum can use the outputs to update its task distribution. You can also update the curriculum directly from the main learner process to incorporate training information. Adding Syllabus's functionality to existing RL training code requires only a few additions.

To use syllabus for your curriculum learning project, you need:

* A curriculum that subclasses `Curriculum` or follows its API.
* An environment that supports multiple tasks.
* A wrapper that subclasses `TaskWrapper` allowing you to set a new task on `reset()`.
* Learning code that uses python multiprocessing or ray actors to parallelize environments.

All of the global coordination is handled automatically by Syllabus's synchronization wrappers.


## Example

This is a simple example of using Syllabus to synchronize a curriculum for CartPole using RLLib. CartPole doesn't normally support multiple tasks so we make a slight modification, allowing us to change the initialization range for the cart (the range from which the cart's initial location is selected). We also implement a `SimpleBoxCurriculum` which increases the initialization range whenever a specific reward threshold is met. We can use the `TaskWrapper` class to implement this new functionality for CartPole and to change the task on `reset()`.

```python
from syllabus import TaskWrapper


class CartPoleTaskWrapper(TaskWrapper):
    def __init__(self, env):
        super().__init__(env)
        self.task = (-0.02, 0.02)
        self.total_reward = 0

    def reset(self, *args, **kwargs):
        self.env.reset()
        self.total_reward = 0
        if "new_task" in kwargs:
            new_task = kwargs.pop("new_task")
            self.change_task(new_task)
        return np.array(self.env.state, dtype=np.float32)

    def change_task(self, new_task):
        low, high = new_task
        self.env.state = self.env.np_random.uniform(low=low, high=high, size=(4,))
        self.task = new_task

    def _task_completion(self, obs, rew, done, info) -> float:
        # Return percent of optimal reward
        self.total_reward += rew
        return self.total_reward / 500.0
```



With just a few modifications to our base learning code, we can train an agent with a curriculum that's globally synchronized across multiple parallel environments.
![Example Diff](./example_diff.png)


As you can see, we just wrap the task-enabled CartPole environment with a `RaySyncWrapper`, and create a curriculum with the `make_ray_curriculum()` function. They automatically communicate with each other to sample tasks from your curriculum, use them in the environments, and update the curriculum with environment outputs. That's it! Now you can implement as many curricula as you want, and as long as they follow the `Curriculum` API, you can hot-swap them in this code.

For more examples, take a look at our examples folder. We currently have examples for the following combinations of RL components:

| RL Library    | Environment                       | Curriculum Method         |
| --------------|-----------------------------------|---------------------------|
| CleanRL       | CartPole-v1 (Gym)                 | SimpleBoxCurriculum       |
| CleanRL       | MiniHack-River-v0 (Gym API)       | PrioritizedLevelReplay    |
| CleanRL       | Pistonball-v6 (Pettingzoo)        | SimpleBoxCurriculum       |
| RLLib         | CartPole-v1 (Gym)                 | SimpleBoxCurriculum       |
| TorchBeast    | NetHackScore-v0 (Gym API)         | LearningProgress          |

If you write any new examples and would like to share them, please create a PR!


# Custom Curricula and Environments
As you can see, adding a curriculum implemented in Syllabus to any environment with a task-wrapper in Syllabus only takes a few lines of code. To create your own curriculum, all you need to do is write a subclass of Syllabus's `Curriculum` class and pass that to the curriculum creation function. `Curriculum` provides multiple methods for updating your curriculum, each meant for a different context.
* `_on_step()` is called once for each environment step by the environment synchronization wrapper.
* `_on_episode()` will be called once for each completed episode  by the environment synchronization wrapper (**not yet implemented**).
* `_complete_task()` is called after each episode  by the environment synchronization wrapper. It receives a boolean or float value indicating whether the selected task was completed in the previous episode.
* `_on_demand()` is meant to be called by the central learner process to update a curriculum with information from the training process, such as TD errors or gradient norms. It is never used by the individual environments.

Your curriculum will probably only use one of these methods, so you can choose to only override the one that you need. If you choose not to use `_on_step()` to update your curriculum, set `update_on_step=False` when initializing the environment synchronization wrapper to improve performance (An exception with the same suggestion is raised by default).

To write a custom task wrapper for an environment, simply subclass the `TaskWrapper` for gym environments or `PettingZooTaskWrapper` for pettingzoo environments. If changing the task only requires you to edit properties of the environment, you can do so in the `change_task()` method. This is called before the internal environment's `reset()` function when you pass a `new_task` to the wrapped environment's `reset()`. If you need to perform more complex operations, you can also override the `reset()` method or other environment methods.

## Task Spaces
Syllabus uses task spaces to define valid ranges for tasks and simplify some logic. These are [Gym spaces](https://gymnasium.farama.org/api/spaces/) which support a majority of existing curriculum methods. For now, the code thoroughly supports Discrete and MultiDiscrete spaces with preliminary support for Box spaces. The task space is typically determined by the environment and limits the type of curriculum that you can use. Extra warnings to clarify these limitations will be added in the future. Most curricula support either a discrete set of tasks or a continuous space of tasks, but not both.


## Optimization
There is a cost to synchronizing separate processes. To minimize this we batch environment step updates, and each communication channel updates independently. That being said, there is still a lot of room to optimize Syllabus. Here is the current speed comparison of environment stepping with and without Syllabus:
```
Relative speed of native multiprocessing with Syllabus: 74.67%
Relative speed Ray multiprocessing with Syllabus: 70.17%
Relative speed of native multiprocessing with Syllabus (no step updates): 90.46%
Relative speed Ray multiprocessing with Syllabus (no step updates): 89.34%
```
As you can see, step updates contribute to a significant slowdown. Not all curricula require individual step outputs, so you can disable these updates in the environment sync wrapper by initializing it with `update_on_step=False`.

Note: This setup means that the environment might sample tasks from the curriculum before the data from its previous episode has been procesed. We assume that this slight delay is inconsequential to most curriculum learning methods.


# Supported Automatic Curriculum Learning Methods:
To help people get started using Syllabus, I've added a few simple curriculum learning methods and some popular baselines (namely Prioritized Level Replay). Below is a full table of supported methods. If you use these methods in your work, please be sure to cite Syllabus as well as original papers and codebases for the relevant methods.

| Method                                | Original Implementation/Citation                  |
| ------------------------------------- | -----------                                       |
| Prioritized Level Replay (PLR)        | https://github.com/facebookresearch/level-replay  |
| Learning Progress                     | https://arxiv.org/abs/2106.14876                  |
| SimpleBoxCurriculum                   |                                                   |


## Citing Syllabus
To be added soon.

# Code heavily based on the original Prioritized Level Replay implementation from https://github.com/facebookresearch/level-replay
# If you use this code, please cite the above codebase and following paper: https://arxiv.org/abs/2010.03934

import numpy as np
import torch
import gym

from collections import namedtuple


class TaskSampler():
    def __init__(self,
                 task_space,
                 action_space,
                 tasks,
                 num_actors=1,
                 strategy='random',
                 replay_schedule='fixed',
                 score_transform='power',
                 temperature=1.0,
                 eps=0.05,
                 rho=0.2,
                 nu=0.5,
                 alpha=1.0,
                 staleness_coef=0,
                 staleness_transform='power',
                 staleness_temperature=1.0
    ):
        self.task_space = task_space
        self.action_space = action_space
        self.tasks = tasks
        self.num_tasks = len(self.tasks)

        self.strategy = strategy
        self.replay_schedule = replay_schedule
        self.score_transform = score_transform
        self.temperature = temperature
        self.eps = eps
        self.rho = rho
        self.nu = nu
        self.alpha = alpha
        self.staleness_coef = staleness_coef
        self.staleness_transform = staleness_transform
        self.staleness_temperature = staleness_temperature

        self.unseen_task_weights = np.array([1.] * self.num_tasks)
        self.task_scores = np.array([0.] * self.num_tasks, dtype=np.float)
        self.partial_task_scores = np.zeros((num_actors, self.num_tasks), dtype=np.float)
        self.partial_task_steps = np.zeros((num_actors, self.num_tasks), dtype=np.int64)
        self.task_staleness = np.array([0.] * self.num_tasks, dtype=np.float)

        self.next_task_index = 0    # Only used for sequential strategy

    def update_with_rollouts(self, rollouts):
        if self.strategy == 'random':
            return

        # Update with a RolloutStorage object
        if self.strategy == 'policy_entropy':
            score_function = self._average_entropy
        elif self.strategy == 'least_confidence':
            score_function = self._average_least_confidence
        elif self.strategy == 'min_margin':
            score_function = self._average_min_margin
        elif self.strategy == 'gae':
            score_function = self._average_gae
        elif self.strategy == 'value_l1':
            score_function = self._average_value_l1
        elif self.strategy == 'one_step_td_error':
            score_function = self._one_step_td_error
        else:
            raise ValueError(f'Unsupported strategy, {self.strategy}')

        self._update_with_rollouts(rollouts, score_function)

    def update_task_score(self, actor_index, task_idx, score, num_steps):
        score = self._partial_update_task_score(actor_index, task_idx, score, num_steps, done=True)

        self.unseen_task_weights[task_idx] = 0.     # No longer unseen

        old_score = self.task_scores[task_idx]
        self.task_scores[task_idx] = (1 - self.alpha)*old_score + self.alpha*score

    def _partial_update_task_score(self, actor_index, task_idx, score, num_steps, done=False):
        partial_score = self.partial_task_scores[actor_index][task_idx]
        partial_num_steps = self.partial_task_steps[actor_index][task_idx]

        running_num_steps = partial_num_steps + num_steps
        merged_score = partial_score + (score - partial_score)*num_steps/float(running_num_steps)

        if done:
            self.partial_task_scores[actor_index][task_idx] = 0.    # zero partial score, partial num_steps
            self.partial_task_steps[actor_index][task_idx] = 0
        else:
            self.partial_task_scores[actor_index][task_idx] = merged_score
            self.partial_task_steps[actor_index][task_idx] = running_num_steps

        return merged_score

    def _average_entropy(self, **kwargs):
        episode_logits = kwargs['episode_logits']
        num_actions = self.action_space.n
        max_entropy = -(1./num_actions)*np.log(1./num_actions)*num_actions

        return (-torch.exp(episode_logits)*episode_logits).sum(-1).mean().item()/max_entropy

    def _average_least_confidence(self, **kwargs):
        episode_logits = kwargs['episode_logits']
        return (1 - torch.exp(episode_logits.max(-1, keepdim=True)[0])).mean().item()

    def _average_min_margin(self, **kwargs):
        episode_logits = kwargs['episode_logits']
        top2_confidence = torch.exp(episode_logits.topk(2, dim=-1)[0])
        return 1 - (top2_confidence[:, 0] - top2_confidence[:,1]).mean().item()

    def _average_gae(self, **kwargs):
        returns = kwargs['returns']
        value_preds = kwargs['value_preds']

        advantages = returns - value_preds

        return advantages.mean().item()

    def _average_value_l1(self, **kwargs):
        returns = kwargs['returns']
        value_preds = kwargs['value_preds']

        advantages = returns - value_preds

        return advantages.abs().mean().item()

    def _one_step_td_error(self, **kwargs):
        rewards = kwargs['rewards']
        value_preds = kwargs['value_preds']

        max_t = len(rewards)
        td_errors = (rewards[:-1] + value_preds[:max_t-1] - value_preds[1:max_t]).abs()
        assert not torch.isnan(td_errors.abs().mean()), f"Got invalid values for 'rewards' or 'value_preds'. Check that reward length: {len(rewards)}"
        return td_errors.abs().mean().item()

    @property
    def requires_value_buffers(self):
        return self.strategy in ['gae', 'value_l1', 'one_step_td_error']

    def _update_with_rollouts(self, rollouts, score_function):
        tasks = rollouts.tasks
        policy_logits = rollouts.action_log_dist
        done = ~(rollouts.masks > 0)
        total_steps, num_actors = policy_logits.shape[:2]

        for actor_index in range(num_actors):
            done_steps = done[:, actor_index].nonzero()[:total_steps, 0]
            start_t = 0

            for t in done_steps:
                if not start_t < total_steps: break

                if t == 0:  # if t is 0, then this done step caused a full update of previous  last cycle
                    continue

                task_idx_t = tasks[start_t, actor_index].item()

                score_function_kwargs = {}
                episode_logits = policy_logits[start_t:t, actor_index]
                score_function_kwargs['episode_logits'] = torch.log_softmax(episode_logits, -1)

                if self.requires_value_buffers:
                    score_function_kwargs['returns'] = rollouts.returns[start_t:t, actor_index]
                    score_function_kwargs['rewards'] = rollouts.rewards[start_t:t, actor_index]
                    score_function_kwargs['value_preds'] = rollouts.value_preds[start_t:t, actor_index]
                score = score_function(**score_function_kwargs)
                num_steps = len(episode_logits)
                self.update_task_score(actor_index, task_idx_t, score, num_steps)

                start_t = t.item()
            if start_t < total_steps:
                # If there is only 1 step, we can't calculate the one-step td error
                if self.strategy == 'one_step_td_error' and start_t == total_steps - 1:
                    continue

                task_idx_t = tasks[start_t, actor_index].item()

                score_function_kwargs = {}
                episode_logits = policy_logits[start_t:, actor_index]
                score_function_kwargs['episode_logits'] = torch.log_softmax(episode_logits, -1)

                if self.requires_value_buffers:
                    score_function_kwargs['returns'] = rollouts.returns[start_t:, actor_index]
                    score_function_kwargs['rewards'] = rollouts.rewards[start_t:, actor_index]
                    score_function_kwargs['value_preds'] = rollouts.value_preds[start_t:, actor_index]

                score = score_function(**score_function_kwargs)
                num_steps = len(episode_logits)
                self._partial_update_task_score(actor_index, task_idx_t, score, num_steps)

    def after_update(self):
        # Reset partial updates, since weights have changed, and thus logits are now stale
        for actor_index in range(self.partial_task_scores.shape[0]):
            for task_idx in range(self.partial_task_scores.shape[1]):
                if self.partial_task_scores[actor_index][task_idx] != 0:
                    self.update_task_score(actor_index, task_idx, 0, 0)
        self.partial_task_scores.fill(0)
        self.partial_task_steps.fill(0)

    def _update_staleness(self, selected_idx):
        if self.staleness_coef > 0:
            self.task_staleness = self.task_staleness + 1
            self.task_staleness[selected_idx] = 0

    def _sample_replay_level(self):
        sample_weights = self.sample_weights()
        if np.isclose(np.sum(sample_weights), 0):
            sample_weights = np.ones_like(sample_weights, dtype=np.float)/len(sample_weights)

        task_idx = np.random.choice(range(self.num_tasks), 1, p=sample_weights)[0]
        task = self.tasks[task_idx]

        self._update_staleness(task_idx)

        return task

    def _sample_unseen_level(self):
        sample_weights = self.unseen_task_weights/self.unseen_task_weights.sum()
        task_idx = np.random.choice(range(self.num_tasks), 1, p=sample_weights)[0]
        task = self.tasks[task_idx]

        self._update_staleness(task_idx)

        return task

    def sample(self, strategy=None):
        if not strategy:
            strategy = self.strategy

        if strategy == 'random':
            task_idx = np.random.choice(range((self.num_tasks)))
            task = self.tasks[task_idx]
            return task

        if strategy == 'sequential':
            task_idx = self.next_task_index
            self.next_task_index = (self.next_task_index + 1) % self.num_tasks
            task = self.tasks[task_idx]
            return task

        num_unseen = (self.unseen_task_weights > 0).sum()
        proportion_seen = (self.num_tasks - num_unseen)/self.num_tasks

        if self.replay_schedule == 'fixed':
            if proportion_seen >= self.rho:
                # Sample replay level with fixed prob = 1 - nu OR if all levels seen
                if np.random.rand() > self.nu or not proportion_seen < 1.0:
                    return self._sample_replay_level()

            # Otherwise, sample a new level
            return self._sample_unseen_level()

        else: # Default to proportionate schedule
            if proportion_seen >= self.rho and np.random.rand() < proportion_seen:
                return self._sample_replay_level()
            else:
                return self._sample_unseen_level()

    def sample_weights(self):
        weights = self._score_transform(self.score_transform, self.temperature, self.task_scores)
        weights = weights * (1-self.unseen_task_weights) # zero out unseen levels
        z = np.sum(weights)
        if z > 0:
            weights /= z

        staleness_weights = 0
        if self.staleness_coef > 0:
            staleness_weights = self._score_transform(self.staleness_transform, self.staleness_temperature, self.task_staleness)
            staleness_weights = staleness_weights * (1-self.unseen_task_weights)
            z = np.sum(staleness_weights)
            if z > 0:
                staleness_weights /= z
            weights = (1 - self.staleness_coef)*weights + self.staleness_coef*staleness_weights
        return weights

    def _score_transform(self, transform, temperature, scores):
        if transform == 'constant':
            weights = np.ones_like(scores)
        if transform == 'max':
            weights = np.zeros_like(scores)
            scores = scores[:]
            scores[self.unseen_task_weights > 0] = -float('inf') # only argmax over seen levels
            argmax = np.random.choice(np.flatnonzero(np.isclose(scores, scores.max())))
            weights[argmax] = 1.
        elif transform == 'eps_greedy':
            weights = np.zeros_like(scores)
            weights[scores.argmax()] = 1. - self.eps
            weights += self.eps/self.num_tasks
        elif transform == 'rank':
            temp = np.flip(scores.argsort())
            ranks = np.empty_like(temp)
            ranks[temp] = np.arange(len(temp)) + 1
            weights = 1/ranks ** (1./temperature)
        elif transform == 'power':
            eps = 0 if self.staleness_coef > 0 else 1e-3
            weights = (np.array(scores) + eps) ** (1./temperature)
        elif transform == 'softmax':
            weights = np.exp(np.array(scores)/temperature)

        return weights
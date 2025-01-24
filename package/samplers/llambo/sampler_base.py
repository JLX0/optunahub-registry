from __future__ import annotations

import time
from typing import Any

from llambo.llambo import LLAMBO
import optuna
from optuna.samplers import RandomSampler
from optuna.samplers._lazy_random_state import LazyRandomState
import optunahub
import pandas as pd


class Sampler(optunahub.samplers.SimpleBaseSampler):
    def __init__(
        self,
        custom_task_description: str = None,
        n_initial_samples: int = 5,
        sm_mode: str = "discriminative",
        num_candidates: int = 10,
        n_templates: int = 2,
        n_gens: int = 10,
        alpha: float = 0.1,
        n_trials: int = 100,
        api_key: str = "",
        model: str = "gpt-4o-mini",
        search_space: dict[str, optuna.distributions.BaseDistribution] | None = None,
        debug: bool = False,
        seed: int | None = None,
    ) -> None:
        """Initialize the sampler with unified parameter handling."""
        super().__init__(search_space)
        self.seed = seed
        self._rng = LazyRandomState(seed)
        self._random_sampler = RandomSampler(seed=seed)  # Retained but not used in sampling
        self.debug = debug
        self.last_time = time.time()
        self.last_trial_count = 0

        # LLAMBO-specific parameters
        self.custom_task_description = custom_task_description
        self.n_initial_samples = n_initial_samples
        self.sm_mode = sm_mode
        self.num_candidates = num_candidates
        self.n_templates = n_templates
        self.n_gens = n_gens
        self.alpha = alpha
        self.n_trials = n_trials
        self.api_key = api_key
        self.model = model

        self.init_observed_fvals = pd.DataFrame()
        self.init_observed_configs = pd.DataFrame()

        self.LLAMBO_instance = None

    def _initialize_llambo(
        self, search_space: dict[str, optuna.distributions.BaseDistribution]
    ) -> None:
        self.hyperparameter_constraints = {}
        for param_name, distribution in search_space.items():
            if isinstance(distribution, optuna.distributions.FloatDistribution):
                dtype = "float"
                dist_type = "log" if distribution.log else "linear"
                bounds = [distribution.low, distribution.high]
            elif isinstance(distribution, optuna.distributions.IntDistribution):
                dtype = "int"
                dist_type = "log" if distribution.log else "linear"
                bounds = [distribution.low, distribution.high]
            elif isinstance(distribution, optuna.distributions.CategoricalDistribution):
                dtype = "categorical"
                dist_type = "categorical"
                bounds = distribution.choices
            else:
                raise ValueError(
                    f"Unsupported distribution type {type(distribution)} for parameter {param_name}"
                )
            self.hyperparameter_constraints[param_name] = [dtype, dist_type, bounds]
        print(f"Hyperparameter constraints: {self.hyperparameter_constraints}")

        # Prepare task_context with dynamic constraints
        task_context = {
            "custom_task_description": self.custom_task_description,
            "lower_is_better": self.lower_is_better,
            "hyperparameter_constraints": self.hyperparameter_constraints,
        }

        sm_mode = self.sm_mode
        top_pct = 0.25 if sm_mode == "generative" else None

        # Initialize LLAMBO with dynamic task context
        self.LLAMBO_instance = LLAMBO(
            task_context,
            sm_mode,
            n_candidates=self.num_candidates,
            n_templates=self.n_templates,
            n_gens=self.n_gens,
            alpha=self.alpha,
            n_initial_samples=self.n_initial_samples,
            n_trials=self.n_trials,
            top_pct=top_pct,
            key=self.api_key,
            model=self.model,
        )

    def _sample_parameters(self) -> dict[str, Any]:
        """Implement your sampler here."""
        sampled_configuration = self.LLAMBO_instance.sample_configurations()
        return sampled_configuration

    def _debug_print(self, message: str) -> None:
        """Print debug message if debug mode is enabled."""
        if self.debug:
            print(message)

    def _calculate_speed(self, n_completed: int) -> None:
        """Calculate and print optimization speed every 100 trials."""
        if not self.debug:
            return

        if n_completed % 100 == 0 and n_completed > 0:
            current_time = time.time()
            elapsed_time = current_time - self.last_time
            trials_processed = n_completed - self.last_trial_count

            if elapsed_time > 0:
                speed = trials_processed / elapsed_time
                print(f"\n[Speed Stats] Trials {self.last_trial_count} to {n_completed}")
                print(f"Speed: {speed:.2f} trials/second")
                print(f"Time elapsed: {elapsed_time:.2f} seconds")
                print("-" * 50)

            self.last_time = current_time
            self.last_trial_count = n_completed

    def reseed_rng(self) -> None:
        """Reseed the random number generator while preserving RandomSampler."""
        self._rng.rng.seed()
        self._random_sampler.reseed_rng()

    def sample_relative(
        self,
        study: optuna.study.Study,
        trial: optuna.trial.FrozenTrial,
        search_space: dict[str, optuna.distributions.BaseDistribution],
    ) -> dict[str, Any]:
        """Unified sampling method for all parameter types."""
        if len(search_space) == 0:
            return {}
            # delegate first trial to random sampler
        self.search_space = search_space

        if trial.number <= self.n_initial_samples:
            if trial.number == 1:
                self.lower_is_better = (
                    True if study.direction == optuna.study.StudyDirection.MINIMIZE else False
                )
                self.init_configs = self.generate_random_samples(
                    search_space, self.n_initial_samples
                )
                self._initialize_llambo(search_space)
            return self.init_configs[trial.number - 1]

        if trial.number == self.n_initial_samples + 1:
            # Pass the observed data from initial trials to initialize LLAMBO
            self.LLAMBO_instance._initialize(
                self.init_configs,
                self.LLAMBO_instance.observed_configs,
                self.LLAMBO_instance.observed_fvals,
            )

        parameters = self._sample_parameters()

        return parameters

    def after_trial(
        self,
        study: optuna.study.Study,
        trial: optuna.trial.FrozenTrial,
        state: optuna.trial.TrialState,
        values: list[float] | None,
    ) -> None:
        """Update the LLAMBO history after a trial is completed."""
        if self.LLAMBO_instance is not None:
            if state == optuna.trial.TrialState.COMPLETE and values is not None:
                self.LLAMBO_instance.update_history(trial.params, values[0])

    def generate_random_samples(
        self, search_space: dict[str, optuna.distributions.BaseDistribution], num_samples: int = 1
    ) -> list[dict[str, Any]]:
        """
        Generate random samples using the RandomSampler's core logic directly.
        """
        samples = []

        for _ in range(num_samples):
            params = {}
            for param_name, distribution in search_space.items():
                # Use the RandomSampler's actual sampling logic
                params[param_name] = self._random_sampler.sample_independent(
                    study=None,
                    trial=None,  # Not actually used by RandomSampler's implementation
                    param_name=param_name,
                    param_distribution=distribution,
                )
            samples.append(params)

        return samples

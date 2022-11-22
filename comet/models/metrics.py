# -*- coding: utf-8 -*-
# Copyright (C) 2020 Unbabel
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

r"""
Metrics
=======
    Regression and Ranking metrics to be used during training to measure 
    correlations with human judgements
"""
from itertools import combinations
from typing import Any, Callable, List, Optional

import pandas as pd
import scipy.stats as stats
import torch
from torch import Tensor
from torchmetrics import Metric, MatthewsCorrCoef


def system_accuracy(y_hat: List[float], y: List[float], system: List[str]) -> float:
    """Implementation of system-level accuracy proposed in
        [To Ship not to Ship](https://aclanthology.org/2021.wmt-1.57/)

    :param y_hat: List of metric scores
    :param y: List of ground truth scores
    :param system: List of systems that produced a given translation.

    :returns: Float with metric accuracy for that data.
    """
    data = pd.DataFrame({"y_hat": y_hat, "y": y, "system": system})
    data = data.groupby("system").mean()
    pairs = list(combinations(data.index.tolist(), 2))

    tp = 0
    for system_a, system_b in pairs:
        human_delta = data.loc[system_a]["y"] - data.loc[system_b]["y"]
        model_delta = data.loc[system_a]["y_hat"] - data.loc[system_b]["y_hat"]
        if (human_delta >= 0) ^ (model_delta < 0):
            tp += 1

    accuracy = tp / len(pairs) if len(pairs) != 0 else 0
    return float(accuracy)


class MCCMetric(MatthewsCorrCoef):
    def __init__(self, prefix: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.prefix = prefix

    def compute(self) -> Tensor:
        """Computes matthews correlation coefficient."""
        mcc = super(MCCMetric, self).compute()
        return {self.prefix + "_mcc": mcc}


class RegressionMetrics(Metric):
    is_differentiable = False
    higher_is_better = True
    preds: List[Tensor]
    target: List[Tensor]

    def __init__(
        self,
        prefix: str = "",
        dist_sync_on_step: bool = False,
        process_group: Optional[Any] = None,
        dist_sync_fn: Optional[Callable] = None,
    ) -> None:
        super().__init__(
            dist_sync_on_step=dist_sync_on_step,
            process_group=process_group,
            dist_sync_fn=dist_sync_fn,
        )
        self.add_state("preds", default=[], dist_reduce_fx="cat")
        self.add_state("target", default=[], dist_reduce_fx="cat")
        self.add_state("systems", default=[], dist_reduce_fx="sum")
        self.prefix = prefix

    def update(
        self,
        preds: Tensor,
        target: Tensor,
        systems: Optional[List[str]] = None,
    ) -> None:  # type: ignore
        """Update state with predictions and targets.
        Args:
            preds: Predictions from model
            target: Ground truth values
        """
        self.preds.append(preds)
        self.target.append(target)

        if systems:
            self.systems += systems

    def compute(self) -> Tensor:
        """Computes spearmans correlation coefficient."""
        preds = torch.cat(self.preds, dim=0)
        target = torch.cat(self.target, dim=0)
        kendall, _ = stats.kendalltau(preds.tolist(), target.tolist())
        spearman, _ = stats.spearmanr(preds.tolist(), target.tolist())
        pearson, _ = stats.pearsonr(preds.tolist(), target.tolist())
        report = {
            self.prefix + "_kendall": kendall,
            self.prefix + "_spearman": spearman,
            self.prefix + "_pearson": pearson,
        }

        if len(self.systems) > 0:
            system_acc = system_accuracy(
                preds.cpu().tolist(), target.cpu().tolist(), self.systems
            )
            report["system_acc"] = system_acc

        return report


class WMTKendall(Metric):
    def __init__(
        self,
        prefix: str = "",
        dist_sync_on_step: bool = False,
        process_group: Optional[Any] = None,
        dist_sync_fn: Optional[Callable] = None,
    ) -> None:
        super().__init__(
            dist_sync_on_step=dist_sync_on_step,
            process_group=process_group,
            dist_sync_fn=dist_sync_fn,
        )
        self.add_state("concordance", default=torch.tensor(0), dist_reduce_fx="sum")
        self.add_state("discordance", default=torch.tensor(0), dist_reduce_fx="sum")
        self.prefix = prefix

    def update(self, distance_pos: torch.Tensor, distance_neg: torch.Tensor):
        assert distance_pos.shape == distance_neg.shape
        self.concordance += torch.sum((distance_pos < distance_neg)).to(
            self.concordance.device
        )
        self.discordance += torch.sum((distance_pos >= distance_neg)).to(
            self.discordance.device
        )

    def compute(self):
        return {
            self.prefix
            + "_kendall": (self.concordance - self.discordance)
            / (self.concordance + self.discordance)
        }

# MIT License
#
# Copyright (C) The Adversarial Robustness Toolbox (ART) Authors 2022
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit
# persons to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the
# Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
# WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""
This module implements certified adversarial training following techniques from works such as:

    | Paper link: http://proceedings.mlr.press/v80/mirman18b/mirman18b.pdf
    | Paper link: https://arxiv.org/pdf/1810.12715.pdf
"""
import logging
import random
import sys

import numpy as np

from sklearn.utils import shuffle
from tqdm import tqdm

from art.defences.trainer.trainer import Trainer
from art.attacks.evasion.projected_gradient_descent.projected_gradient_descent import ProjectedGradientDescent
from art.utils import check_and_transform_label_format

if sys.version_info >= (3, 8):
    from typing import TypedDict, List, Optional, Any, Tuple, Union, TYPE_CHECKING
else:
    from typing import Dict, List, Optional, Any, Tuple, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from art.utils import IBP_CERTIFIER_TYPE

    # if we use python 3.8 or higher use the more informative TypedDict for type hinting
    if sys.version_info >= (3, 8):

        class PGDParamDict(TypedDict):
            """
            A TypedDict class to define the types in the pgd_params dictionary.
            """

            eps: float
            eps_step: float
            max_iter: int
            num_random_init: int
            batch_size: int

    else:
        PGDParamDict: Dict[str, Union[int, float]]


logger = logging.getLogger(__name__)


class DefaultLinearScheduler:
    """
    Class implementing a simple linear scheduler to grow the certification radius or change the loss weightings
    """

    def __init__(self, step_per_epoch: float, initial_val: float = 0.0, warmup: int = 0) -> None:
        """
        Create a .DefaultLinearScheduler instance.

        :param step_per_epoch: how much to increase the certification radius every epoch
        :param initial_val: the initial value
        :param warmup: if to have an initial warmup period
        """
        self.step_per_epoch = step_per_epoch
        self.val = initial_val
        self.warmup = warmup

    def step(self, epoch) -> float:
        """
        Grow the value by self.step_per_epoch
        """
        if epoch > self.warmup:
            self.val += self.step_per_epoch
        return self.val


class AdversarialTrainerCertifiedPytorchIBP(Trainer):
    """
    Class performing certified adversarial training from methods such as

    | Paper link: http://proceedings.mlr.press/v80/mirman18b/mirman18b.pdf
    | Paper link: https://arxiv.org/pdf/1810.12715.pdf
    """

    def __init__(
        self,
        classifier: "IBP_CERTIFIER_TYPE",
        nb_epochs: Optional[int] = 20,
        bound: float = 0.1,
        loss_weighting: float = 0.1,
        batch_size: int = 32,
        use_certification_schedule: bool = True,
        certification_schedule: Optional[Any] = None,
        augment_with_pgd: bool = None,
        pgd_params: Optional["PGDParamDict"] = None,
    ) -> None:
        """
        Create an :class:`.AdversarialTrainerCertified` instance.

        Default values are for MNIST in pixel range 0-1.

        :param classifier: Classifier to train adversarially.
        :param pgd_params: A dictionary containing the specific parameters relating to regular PGD training.
                           If not provided, we will default to typical MNIST values.
                           Otherwise must contain the following keys:

                           * *eps*: Maximum perturbation that the attacker can introduce.
                           * *eps_step*: Attack step size (input variation) at each iteration.
                           * *max_iter*: The maximum number of iterations.
                           * *batch_size*: Size of the batch on which adversarial samples are generated.
                           * *num_random_init*: Number of random initialisations within the epsilon ball.
        :param bound: The perturbation range for the zonotope. Will be ignored if a certification_schedule is used.
        :param loss_weighting: Weighting factor for the certified loss.
        :param nb_epochs: Number of training epochs.
        :param use_certification_schedule: If to use a training schedule for the certification radius.
        :param certification_schedule: Schedule for gradually increasing the certification radius. Empirical studies
                                       have shown that this is often required to achieve best performance.
                                       Either True to use the default linear scheduler,
                                       or a class with a .step() method that returns the updated bound every epoch.
        :param batch_size: Size of batches to use for certified training.
        """
        super().__init__(classifier=classifier)
        self._classifier: "IBP_CERTIFIER_TYPE"
        self.pgd_params: "PGDParamDict"
        self.nb_epochs = nb_epochs
        self.loss_weighting = loss_weighting
        self.bound = bound
        self.use_certification_schedule = use_certification_schedule
        self.certification_schedule = certification_schedule
        self.batch_size = batch_size
        self.augment_with_pgd = augment_with_pgd

        if self.augment_with_pgd:

            if pgd_params is None:
                self.pgd_params = {"eps": 0.3, "eps_step": 0.05, "max_iter": 2, "batch_size": 128, "num_random_init": 1}
            else:
                self.pgd_params = pgd_params

            # Setting up adversary and perform adversarial training:
            self.attack = ProjectedGradientDescent(
                estimator=self._classifier,
                eps=self.pgd_params["eps"],
                eps_step=self.pgd_params["eps_step"],
                max_iter=self.pgd_params["max_iter"],
                num_random_init=self.pgd_params["num_random_init"],
            )

    def fit(  # pylint: disable=W0221
        self,
        x: np.ndarray,
        y: np.ndarray,
        certification_loss: Any = "interval_loss_cce",
        batch_size: Optional[int] = None,
        nb_epochs: Optional[int] = None,
        training_mode: bool = True,
        scheduler: Optional[Any] = None,
        verbose: bool = True,
        **kwargs,
    ) -> None:
        """
        Fit the classifier on the training set `(x, y)`.

        :param x: Training data.
        :param y: Target values (class labels) one-hot-encoded of shape (nb_samples, nb_classes) or index labels of
                  shape (nb_samples,).
        :param certification_loss: Which certification loss function to use. Either "interval_loss_cce"
                                   or "max_logit_loss". By default will use interval_loss_cce.
                                   Alternatively, a user can supply their own loss function which takes in as input
                                   the zonotope predictions of the form () and labels of the from () and returns a
                                   scalar loss.
        :param batch_size: Size of batches to use for certified training. NB, this will run the data
                           sequentially accumulating gradients over the batch size.
        :param nb_epochs: Number of epochs to use for training.
        :param training_mode: `True` for model set to training mode and `'False` for model set to evaluation mode.
        :param scheduler: Learning rate scheduler to run at the start of every epoch.
        :param verbose: If to display the per-batch statistics while training.
        :param kwargs: Dictionary of framework-specific arguments. This parameter is not currently supported for PyTorch
               and providing it takes no effect.
        """
        import torch

        if batch_size is None:
            batch_size = self.batch_size

        if nb_epochs is not None:
            epochs: int = nb_epochs
        elif self.batch_size is not None:
            epochs = self.batch_size
        else:
            raise ValueError("Value of `epochs` not defined.")

        # Set model mode
        self._classifier._model.train(mode=training_mode)  # pylint: disable=W0212

        if self._classifier._optimizer is None:  # pragma: no cover # pylint: disable=W0212
            raise ValueError("An optimizer is needed to train the model, but none is provided.")

        y = check_and_transform_label_format(y, nb_classes=self._classifier.nb_classes)

        # Apply preprocessing
        x_preprocessed, y_preprocessed = self._classifier.apply_preprocessing(x, y, fit=True)

        # Check label shape
        y_preprocessed = self._classifier.reduce_labels(y_preprocessed)

        num_batch = int(np.ceil(len(x_preprocessed) / float(batch_size)))
        ind = np.arange(len(x_preprocessed))

        x_cert = np.copy(x_preprocessed)
        y_cert = np.copy(y_preprocessed)

        # Start training
        if self.use_certification_schedule:
            if self.certification_schedule is None:
                # Using MNIST values as default.
                warm_up = int(0.03 * epochs)
                epochs_to_ramp = int(0.15 * epochs)  # lowering k from 1 to 0.5 over 15% of the total epochs
                                                     # and increasing eps from 0 to eps
                if epochs_to_ramp == 0:
                    epochs_to_ramp = 1

                initial_k = 0.0
                final_k = 0.5
                step_in_k_per_epoch = (final_k - initial_k) / epochs_to_ramp

                initial_eps = 0.2
                final_eps = self.bound
                step_in_eps_per_epoch = (final_eps - initial_eps) / epochs_to_ramp

                certification_schedule_function = DefaultLinearScheduler(
                    step_per_epoch=step_in_eps_per_epoch, initial_val=0.0
                )
                loss_weighting_schedule_function = DefaultLinearScheduler(
                    step_per_epoch=step_in_k_per_epoch,
                    initial_val=initial_k,
                    warmup=warm_up,
                )
        else:
            bound = self.bound

        for epoch_num in tqdm(range(epochs)):
            if self.use_certification_schedule:
                bound = certification_schedule_function.step(epoch_num)
                loss_weighting_k = loss_weighting_schedule_function.step(epoch_num)

            # Shuffle the examples
            random.shuffle(ind)

            # Train for one epoch
            pbar = tqdm(range(num_batch), disable=not verbose)
            x_cert, y_cert = shuffle(x_cert, y_cert)

            for m in pbar:
                x_batch = np.copy(x_cert[batch_size * m:batch_size * (m + 1)])
                y_batch = np.copy(y_cert[batch_size * m:batch_size * (m + 1)])

                # Zero the parameter gradients
                self._classifier._optimizer.zero_grad()  # pylint: disable=W0212

                self.set_forward_mode("concrete")
                concrete_pred = self._classifier.model.forward(x_batch)
                concrete_pred = torch.argmax(concrete_pred)

                # TODO: Allow passing of custom functions here
                processed_x_cert = self._classifier.concrete_to_interval(x_batch, bound, limits=[0.0, 1.0])

                # Perform prediction
                self.set_forward_mode("abstract")
                interval_preds = self._classifier.model.forward(processed_x_cert)

                # TODO: Change to work with intervals
                if certification_loss == "max_logit_loss":
                    # certified_loss = self._classifier.max_logit_loss(
                    #    prediction=interval_preds, target=y_batch
                    #)
                    pass
                elif certification_loss == "interval_loss_cce":
                    certified_loss = self._classifier.interval_loss_cce(
                        prediction=interval_preds,
                        target=torch.from_numpy(y_batch).to(self._classifier.device),
                    )
                else:
                    certified_loss = certification_loss(interval_preds, y_batch)
                samples_certified = self._classifier.certify(preds=interval_preds.cpu().detach(), labels=y_batch)

                # Concrete PGD loss
                if self.augment_with_pgd:
                    i_batch = np.copy(
                        x_preprocessed[ind[m * self.pgd_params["batch_size"] : (m + 1) * self.pgd_params["batch_size"]]]
                    ).astype("float32")
                    o_batch = y_preprocessed[
                        ind[m * self.pgd_params["batch_size"] : (m + 1) * self.pgd_params["batch_size"]]
                    ]
                    # Perform prediction
                    self.set_forward_mode("attack")
                    self.attack = ProjectedGradientDescent(
                        estimator=self._classifier,
                        eps=self.pgd_params["eps"],
                        eps_step=self.pgd_params["eps_step"],
                        max_iter=self.pgd_params["max_iter"],
                        num_random_init=self.pgd_params["num_random_init"],
                    )
                    i_batch = self.attack.generate(i_batch, y=o_batch)
                    self._classifier.model.zero_grad()
                else:
                    i_batch = np.copy(
                        x_preprocessed[ind[m * batch_size : (m + 1) * batch_size]]
                    ).astype("float32")
                    o_batch = y_preprocessed[
                        ind[m * batch_size : (m + 1) * batch_size]
                    ]
                self.set_forward_mode("concrete")

                model_outputs = self._classifier.model.forward(i_batch)
                acc = self._classifier.get_accuracy(model_outputs, o_batch)

                # Form the loss function
                pgd_loss = self._classifier.concrete_loss(
                    model_outputs, torch.from_numpy(o_batch).to(self._classifier.device)
                )  # pylint: disable=W0212

                if verbose:
                    pbar.set_description(
                        f"Bound {bound:.2f}: Loss {pgd_loss:.2f} Cert Loss {certified_loss:.2f} "
                        f"Acc {acc:.2f} Cert Acc {np.sum(samples_certified) / batch_size:.2f} "
                        f"loss weighting {loss_weighting_k:.2f}"
                    )

                loss = certified_loss * loss_weighting_k + pgd_loss * (1 - loss_weighting_k)
                # Do training
                if self._classifier._use_amp:  # pragma: no cover # pylint: disable=W0212
                    from apex import amp  # pylint: disable=E0611

                    with amp.scale_loss(loss, self._classifier._optimizer) as scaled_loss:  # pylint: disable=W0212
                        scaled_loss.backward()

                else:
                    loss.backward()

                self._classifier._optimizer.step()  # pylint: disable=W0212
                self._classifier.model.re_convert()
            if scheduler is not None:
                scheduler.step()

    def predict(self, x: np.ndarray, **kwargs) -> np.ndarray:
        """
        Perform prediction using the adversarially trained classifier.

        :param x: Input samples.
        :param kwargs: Other parameters to be passed on to the `predict` function of the classifier.
        :return: Predictions for test set.
        """
        if self._classifier._model._model.forward_mode != "concrete":  # pylint: disable=W0212
            raise ValueError(
                "For normal predictions, the model must be running in concrete mode. If an abstract "
                "prediction is wanted then use predict_interval instead"
            )

        return self._classifier.predict(x, **kwargs)

    def predict_interval(self, cent: np.ndarray, bound, **kwargs) -> Tuple[List[np.ndarray], List[np.ndarray]]:
        """
        Perform prediction using the adversarially trained classifier using zonotopes

        :param cent: The datapoint, representing the zonotope center.
        :param bound: The perturbation range for the zonotope.
        """

        if self._classifier._model._model.forward_mode != "abstract":  # pylint: disable=W0212
            raise ValueError(
                "For interval predictions, the model must be running in abstract mode. If a concrete "
                "prediction is wanted then use predict instead"
            )

        return self._classifier.predict_zonotopes(cent, bound, **kwargs)

    def set_forward_mode(self, mode: str) -> None:
        """
        Helper function to set the forward mode of the model

        :param mode: either concrete or abstract signifying how to run the forward pass
        """
        self._classifier._model._model.set_forward_mode(mode)  # pylint: disable=W0212

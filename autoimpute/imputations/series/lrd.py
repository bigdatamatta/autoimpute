"""This module implements local residual draws via the LRDImputer.

This module contains the LRDImputer, which takes local residual draws to
impute missing values. LRD is similar to PMM, but residuals are added onto
the selected value for imputation. Dataframe imputers utilize this class when
its strategy is requested. Use SingleImputer or MultipleImputer with
strategy = `lrd` to broadcast the strategy across all the columns in a
dataframe, or specify this strategy for a given column.
"""

import numpy as np
import pymc3 as pm
from pandas import DataFrame
from scipy.stats import multivariate_normal
from sklearn.linear_model import LinearRegression
from sklearn.utils.validation import check_is_fitted
from autoimpute.imputations import method_names
from autoimpute.imputations.errors import _not_num_series
from autoimpute.imputations.helpers import _local_residuals
from .base import ISeriesImputer
methods = method_names
# pylint:disable=attribute-defined-outside-init
# pylint:disable=no-member
# pylint:disable=unused-variable

class LRDImputer(ISeriesImputer):
    """Impute missing values using local residual draws.

    The LRDImputer produces predictions using a combination of bayesian
    approach to least squares and least squares itself. For each missing value
    LRD finds the `n` closest neighbors from a least squares regression
    prediction set, and samples from the corresponding true values for y of
    each of those `n` predictions. The imputation is the resulting sample plus
    the residual, or the distance between the prediction and the neighbor.
    To implement bayesian least squares, the imputer utlilizes the pymc3
    library. The imputer can be used directly, but such behavior is
    discouraged. LRDImputer does not have the flexibility / robustness of
    dataframe imputers, nor is its behavior identical. Preferred use is
    MultipleImputer(strategy="lrd").
    """
    # class variables
    strategy = methods.LRD

    def __init__(self, am=None, asd=10, bm=None, bsd=10, sig=1, sample=1000,
                 tune=1000, init="auto", fill_value="random", neighbors=5,
                 **kwargs):
        """Create an instance of the LRDImputer class.

        The class requires multiple arguments necessary to create priors for
        a bayesian linear regression equation and least squares itself.
        Therefore, LRD arguments include all of those seen in bayesian least
        squares and least squares itself. New parameters include `neighbors`,
        or the number of neighbors that LRD uses to sample observed.

        Args:
            am (float, Optional): mean of alpha prior. Default 0.
            asd (float, Optional): std. deviation of alpha prior. Default 10.
            bm (float, Optional): mean of beta priors. Default 0.
            bsd (float, Optional): std. deviation of beta priors. Default 10.
            sig (float, Optional): parameter of sigma prior. Default 1.
            sample (int, Optional): number of posterior samples per chain.
                Default = 1000. More samples, longer to run, but better
                approximation of the posterior & chance of convergence.
            tune (int, Optional): parameter for tuning. Draws done in addition
                to sample. Default = 1000.
            init (str, Optional): MCMC algo to use for posterior sampling.
                Default = 'auto'. See pymc3 docs for more info on choices.
            fill_value (str, Optional): How to draw from the posterior to
                create imputations. Default is "random". 'random' and 'mean'
                supported for explicit options.
            neighbors (int, Optional): number of neighbors. Default is 5.
                Value should be greater than 0 and less than # observed,
                although anything greater than 10-20 generally too high
                unless dataset is massive.
            **kwargs: key value arguments passed to linear regression
        """
        self.am = am
        self.asd = asd
        self.bm = bm
        self.bsd = bsd
        self.sig = sig
        self.sample = sample
        self.tune = tune
        self.init = init
        self.fill_value = fill_value
        self.neighbors = neighbors
        self.lm = LinearRegression(**kwargs)

    def fit(self, X, y):
        """Fit the Imputer to the dataset by fitting bayesian and LS model.

        Args:
            X (pd.Dataframe): dataset to fit the imputer.
            y (pd.Series): response, which is eventually imputed.

        Returns:
            self. Instance of the class.
        """
        _not_num_series(self.strategy, y)
        nc = len(X.columns)

        # get predictions for the data, which will be used for "closest" vals
        y_pred = self.lm.fit(X, y).predict(X)
        y_df = DataFrame({"y": y, "y_pred": y_pred})

        # calculate bayes and use appropriate means for alpha and beta priors
        # here we specify the point estimates from the linear regression as the
        # means for the priors. This will greatly speed up posterior sampling
        # and help ensure that convergence occurs
        if self.am is None:
            self.am = self.lm.intercept_
        if self.bm is None:
            self.bm = self.lm.coef_

        # initialize model for bayesian linear reg. Default vals for priors
        # assume data is scaled and centered. Convergence can struggle or fail
        # if not the case and proper values for the priors are not specified
        # separately, also assumes each beta is normal and "independent"
        # while betas likely not independent, this is technically a rule of OLS
        with pm.Model() as fit_model:
            alpha = pm.Normal("alpha", self.am, sd=self.asd)
            beta = pm.Normal("beta", self.bm, sd=self.bsd, shape=nc)
            sigma = pm.HalfCauchy("σ", self.sig)
            mu = alpha+beta.dot(X.T)
            score = pm.Normal("score", mu, sd=sigma, observed=y)
        params = {"model": fit_model, "y_obs": y_df}
        self.statistics_ = {"param": params, "strategy": self.strategy}
        return self

    def impute(self, X):
        """Generate imputations using predictions from the fit bayesian model.

        The transform method returns the values for imputation. Missing values
        in a given dataset are replaced with the random selection from the LRD
        process. Again, LRD imputes actually observed values, and the observed
        values are selected by finding the closest least squares predictions
        to a given prediction from the bayesian model.

        Args:
            X (pd.DataFrame): predictors to determine imputed values.

        Returns:
            np.array: imputed dataset.
        """
        # check if fitted then predict with least squares
        check_is_fitted(self, "statistics_")
        model = self.statistics_["param"]["model"]
        df = self.statistics_["param"]["y_obs"]
        df = df.reset_index(drop=True)

        # generate posterior distribution for alpha, beta coefficients
        with model:
            tr = pm.sample(
                sample=self.sample,
                tune=self.tune,
                init=self.init,
            )
        self.trace_ = tr

        # sample random alpha from alpha posterior distribution
        # get the mean and covariance of the multivariate betas
        # betas assumed multivariate normal by linear reg rules
        # sample beta w/ cov structure to create realistic variability
        alpha_bayes = np.random.choice(tr["alpha"])
        beta_means = tr["beta"].mean(0)
        beta_cov = np.cov(tr["beta"].T)
        beta_bayes = np.array(multivariate_normal(beta_means, beta_cov).rvs())

        # predictions for missing y, using bayes alpha + coeff samples
        # use these preds for nearest neighbor search from reg results
        # neighbors are nearest from prediction model fit on observed
        # imputed values are actual y vals corresponding to nearest neighbors
        # therefore, this is a form of "hot-deck" imputation
        y_pred_bayes = alpha_bayes + beta_bayes.dot(X.T)
        n_ = self.neighbors
        if X.columns.size == 1:
            y_pred_bayes = y_pred_bayes[0]
        if self.fill_value == "mean":
            imp = [_local_residuals(x, n_, df, np.mean) for x in y_pred_bayes]
        elif self.fill_value == "random":
            choice = np.random.choice
            imp = [_local_residuals(x, n_, df, choice) for x in y_pred_bayes]
        else:
            err = f"{self.fill_value} must be `mean` or `random`."
            raise ValueError(err)

        # finally, set last class values and return imputations
        self.y_pred = y_pred_bayes
        self.alphas = alpha_bayes
        self.betas = beta_bayes
        return imp

    def fit_impute(self, X, y):
        """Fit impute method to generate imputations where y is missing.

        Args:
            X (pd.Dataframe): predictors in the dataset.
            y (pd.Series): response w/ missing values to impute.

        Returns:
            np.array: imputed dataset.
        """
        # transform occurs with records from X where y is missing
        miss_y_ix = y[y.isnull()].index
        return self.fit(X, y).impute(X.loc[miss_y_ix])

"""This module uses available information in a dataset to predict imputations.

This module contains one class - the PredictiveImputer. Use this class to
predict imputations for each Series within a DataFrame using all or a subset
of the other available features. This class extends the behavior of the
SingleImputer. Unlike the SingleImputer, the supported methods in this class
are multivariate - they use more than just the series itself to determine the
best estimated values for imputaiton.
"""

import pandas as pd
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.utils.validation import check_is_fitted
from autoimpute.utils import check_nan_columns
from autoimpute.imputations import method_names
from autoimpute.imputations.dataframe import predictive_methods
from .base_imputer import BaseImputer
from .single_imputer import SingleImputer
methods = method_names
pm = predictive_methods

# pylint:disable=attribute-defined-outside-init
# pylint:disable=arguments-differ
# pylint:disable=protected-access
# pylint:disable=too-many-arguments
# pylint:disable=too-many-locals
# pylint:disable=too-many-instance-attributes

class PredictiveImputer(BaseImputer, BaseEstimator, TransformerMixin):
    """Techniques to impute Series with missing values through learning.

    The PredictiveImputer class takes a DataFrame and performs imputations
    on each Series within the DataFrame. The Predictive does one pass for
    each column, and it supports multivariate methods for each column.

    The class is a valid transformer that can be used in an sklearn pipeline
    because it inherits from the TransformerMixin and implements both fit and
    transform methods.

    All of the imputers are inductive (i.e. fit and transform for new data).
    That being said, the `fit` stage deviates from what one might expect in a
    few cases. The `predictive_method` module goes into detail of each method.

    Attributes:
        strategies (dict): dictionary of supported imputation methods.
            Key = imputation name; Value = function to perform imputation.
            `default` imputes pmm for numerical, logistic for categorical.
            `least squares` predict missing values from linear regression.
            `binary logistic` predict missing values with 2 classes.
            `multinomial logistic` predict missing values with multiclass.
            `stochastic` linear regression + random draw from norm w/ mse std.
            `bayesian least squares` draw from the posterior predictive
                distribution for each missing value, using underlying OLS.
            `bayesian binary logistic` draw from the posterior predictive
                distribution for each missing value, using underling logistic.
            `pmm` imputes series using predictive mean matching. PMM is a
                semi-supervised method using bayesian & hot-deck imputation.
    """

    strategies = {
        methods.DEFAULT: pm._predictive_default,
        methods.LS: pm._fit_least_squares_reg,
        methods.BINARY_LOGISTIC: pm._fit_binary_logistic_reg,
        methods.MULTI_LOGISTIC: pm._fit_multi_logistic_reg,
        methods.STOCHASTIC: pm._fit_stochastic_reg,
        methods.BAYESIAN_LS: pm._fit_bayes_least_squares_reg,
        methods.BAYESIAN_BINARY_LOGISTIC: pm._fit_bayes_binary_logistic_reg,
        methods.PMM: pm._fit_pmm_reg
    }

    def __init__(self, strategy="default", predictors="all",
                 fill_value=None, copy=True, scaler=None, verbose=None):
        """Create an instance of the PredictiveImputer class.

        As with sklearn classes, all arguments take default values. Therefore,
        PredictiveImputer creates a valid class instance. The instance is used
        to set up an imputer and perform checks on arguments.

        Args:
            strategy (str, iter, dict; optional): strategies for imputation.
                Default value is str -> "default". I.e. default imputation.
                If str, single strategy broadcast to all series in DataFrame.
                If iter, must provide 1 strategy per column. Each method within
                iterator applies to column with same index value in DataFrame.
                If dict, must provide key = column name, value = imputer.
                Dict the most flexible and PREFERRED way to create custom
                imputation strategies if not using the default. Dict does not
                require method for every column; just those specified as keys.
            predictors (str, iter, dict, optiona): defaults to all, i.e.
                use all predictors. If all, every column will be used for
                every class prediction. If a list, subset of columns used for
                all predictions. If a dict, specify which columns to use as
                predictors for each imputation. Columns not specified in dict
                but present in `strategy` receive `all` other cols as preds.
            fill_value (str, optional): fill val when strategy needs more info.
                See details of individual strategies for more info.
            copy (bool, optional): create copy of DataFrame or operate inplace.
                Default value is True. Copy created.
            scaler (scaler, optional): scale variables before transformation.
                Default is None, although StandardScaler recommended.
            verbose (bool, optional): print more information to console.
                Default value is False.
        """

        BaseImputer.__init__(
            self,
            imp_kwgs=None,
            scaler=scaler,
            verbose=verbose
        )
        self.strategy = strategy
        self.predictors = predictors
        self.fill_value = fill_value
        self.copy = copy

    @property
    def strategy(self):
        """Property getter to return the value of the strategy property."""
        return self._strategy

    @strategy.setter
    def strategy(self, s):
        """Validate the strategy property to ensure it's Type and Value.

        Class instance only possible if strategy is proper type, as outlined
        in the init method. Passes supported strategies and user arg to
        helper method, which performs strategy checks.

        Args:
            s (str, iter, dict): Strategy passed as arg to class instance.

        Raises:
            ValueError: Strategies not valid (not in allowed strategies).
            TypeError: Strategy must be a string, tuple, list, or dict.
            Both errors raised through helper method `check_strategy_allowed`.
        """
        strat_names = self.strategies.keys()
        self._strategy = self.check_strategy_allowed(strat_names, s)

    def _fit_strategy_validator(self, X):
        """Internal helper method to validate strategies appropriate for fit.

        Checks whether strategies match with type of column they are applied
        to. If not, error is raised through `check_strategy_fit` method.
        """
        # remove nan columns and store colnames
        cols = X.columns.tolist()
        self._strats = self.check_strategy_fit(self.strategy, cols)
        self._preds = self.check_predictors_fit(self.predictors, cols)

        # next, prep the categorical / numerical split
        # only necessary for classes that use other features during imputation
        # wont see this requirement in the single imputer
        self._prep_fit_dataframe(X)

        # if scaler passed, need scaler to fit AND transform
        # we want to fit predictive imputer on correctly scaled dataset
        if self.scaler:
            self._scaler_fit_transform()

    def _transform_strategy_validator(self, X, new_data):
        """Private method to prep for prediction."""
        # initial checks before transformation
        check_is_fitted(self, "statistics_")

        # check columns are the same
        X_cols = X.columns.tolist()
        fit_cols = set(self._strats.keys())
        diff_fit = set(fit_cols).difference(X_cols)
        if diff_fit:
            err = "Same columns that were fit must appear in transform."
            raise ValueError(err)

        # if not error, check if new data and perform scale if necessary
        # note that this step is crucial if using fit and transform separately
        # when used separately, new data needs to be prepped again
        if new_data:
            self._prep_fit_dataframe(X)
            if self.scaler:
                self._scaler_transform()

    @check_nan_columns
    def fit(self, X):
        """Fit imputation methods to each column within a DataFrame.

        The fit method calclulates the `statistics` necessary to later
        transform a dataset (i.e. perform actual imputatations). Inductive
        methods calculate statistic on the fit data, then impute new missing
        data with that value. All currently supported methods are inductive.

        Args:
            X (pd.DataFrame): pandas DataFrame on which imputer is fit.

        Returns:
            self: instance of the PredictiveImputer class.
        """
        # first, prep columns we plan to use and make sure they are valid
        self._fit_strategy_validator(X)
        self.statistics_ = {}

        # header print statement if verbose = true
        if self.verbose:
            ft = "FITTING IMPUTATION METHODS TO DATA..."
            st = "Strategies & Predictors used to fit each column:"
            print(f"{ft}\n{st}\n{'-'*len(st)}")

        # perform fit on each column, depending on that column's strategy
        # note - because we use predictors, logic more involved than single
        for col_name, func_name in self._strats.items():
            f = self.strategies[func_name]
            x, _ = self._prep_predictor_cols(col_name, self._preds)
            y = X[col_name]
            fit_param, fit_name = f(x, y, self.verbose)
            self.statistics_[col_name] = {"param": fit_param,
                                          "strategy": fit_name}
            # print strategies if verbose
            if self.verbose:
                resp = f"Response: {col_name}"
                preds = f"Predictors: {self._preds[col_name]}"
                strat = f"Strategy {fit_name}"
                print(f"{resp}\n{preds}\n{strat}\n{'-'*len(st)}")
        return self

    @check_nan_columns
    def transform(self, X, new_data=True):
        """Impute each column within a DataFrame using fit imputation methods.

        The transform step performs the actual imputations. Given a dataset
        previously fit, `transform` imputes each column with it's respective
        imputed values from fit (in the case of inductive) or performs new fit
        and transform in one sweep (in the case of transductive).

        Args:
            X (pd.DataFrame): fit DataFrame to impute.
            new_data (bool, Optional): whether or not new data is used.
                Default is False.

        Returns:
            X (pd.DataFrame): imputed in place or copy of original.

        Raises:
            ValueError: same columns must appear in fit and transform.
        """
        # copy the dataset if necessary, then prep predictors
        if self.copy:
            X = X.copy()
        self._transform_strategy_validator(X, new_data)
        if self.verbose:
            trans = "PERFORMING IMPUTATIONS ON DATA BASED ON FIT..."
            print(f"{trans}\n{'-'*len(trans)}")

        # transformation logic
        self.imputed_ = {}
        self.traces_ = {}
        for col_name, fit_data in self.statistics_.items():
            strat = fit_data["strategy"]
            fill = fit_data["param"]
            imp_ix = X[col_name][X[col_name].isnull()].index
            self.imputed_[col_name] = imp_ix.tolist()
            if self.verbose:
                nimp = len(imp_ix)
                print(f"Numer of imputations to perform: {nimp}")
                if nimp > 0:
                    print(f"Transforming {col_name} with strategy '{strat}'")
                else:
                    print(f"No imputations, moving to next column...")

            # continue if there are no imputations to make
            if imp_ix.empty:
                continue
            x, _ = self._prep_predictor_cols(col_name, self._preds)
            if new_data:
                x.index = self._X_idx
            x = x.loc[imp_ix, :]

            # may abstract SingleImputer in future for flexibility
            mis_cov = pd.isnull(x).sum()
            if any(mis_cov):
                if self.verbose:
                    print(f"Missing Covariates:\n{mis_cov}\n")
                    print("Using single imputer for missing covariates...")
                x = SingleImputer(
                    verbose=self.verbose, copy=False
                ).fit_transform(x)

            # fill missing values based on the method selected
            # note that default picks a method below depending on col
            # -------------------------------------------------------
            # linear regression imputation
            if strat == methods.LS:
                pm._imp_least_squares_reg(X, col_name, x, fill, imp_ix)
            if strat in (methods.BINARY_LOGISTIC, methods.MULTI_LOGISTIC):
                pm._imp_logistic_reg(X, col_name, x, fill, imp_ix)
            if strat == methods.STOCHASTIC:
                pm._imp_stochastic_reg(X, col_name, x, fill, imp_ix)
            if strat == methods.BAYESIAN_LS:
                tr = pm._imp_bayes_least_squares_reg(
                    X, col_name, x, fill, imp_ix, self.fill_value, self.verbose
                )
                self.traces_[col_name] = tr
            if strat == methods.BAYESIAN_BINARY_LOGISTIC:
                tr = pm._imp_bayes_logistic_reg(
                    X, col_name, x, fill, imp_ix, self.fill_value, self.verbose
                )
                self.traces_[col_name] = tr
            if strat == methods.PMM:
                tr = pm._imp_pmm_reg(
                    X, col_name, x, fill, imp_ix, self.fill_value, self.verbose
                )
                self.traces_[col_name] = tr
            if strat == methods.NONE:
                pass
        return X

    def fit_transform(self, X):
        """Convenience method to fit then transform the same dataset."""
        return self.fit(X).transform(X, False)
import numpy as np
import pandas as pd
import openml
import lccv
import os, psutil
import gc
import logging
import traceback

from func_timeout import func_timeout, FunctionTimedOut

import time
import random

import itertools as it
import scipy.stats
from scipy.sparse import lil_matrix
from sklearn.pipeline import Pipeline
from sklearn.compose import ColumnTransformer

import sklearn
from sklearn import metrics
from sklearn import *

from func_timeout import func_timeout, FunctionTimedOut
from commons import *
from tqdm import tqdm

eval_logger = logging.getLogger("evalutils")


def get_dataset(openmlid):
    ds = openml.datasets.get_dataset(openmlid)
    df = ds.get_data()[0]
    num_rows = len(df)
        
    # prepare label column as numpy array
    print(f"Read in data frame. Size is {len(df)} x {len(df.columns)}.")
    X = df.drop(columns=[ds.default_target_attribute]).values
    y = df[ds.default_target_attribute].values
    print(f"Data is of shape {X.shape}.")
    return X, y




def format_learner(learner):
    learner_name = str(learner).replace("\n", " ").replace("\t", " ")
    for k in  range(20):
        learner_name = learner_name.replace("  ", " ")
    return learner_name

    
class Evaluator:
    
    def __init__(self, X, y):
        self.X = X
        self.y = y
        
        # determine fixed pre-processing steps for imputation and binarization
        types = [set([type(v) for v in r]) for r in X.T]
        numeric_features = [c for c, t in enumerate(types) if len(t) == 1 and list(t)[0] != str]
        numeric_transformer = Pipeline([("imputer", sklearn.impute.SimpleImputer(strategy="median"))])
        categorical_features = [i for i in range(X.shape[1]) if i not in numeric_features]
        missing_values_per_feature = np.sum(pd.isnull(X), axis=0)
        eval_logger.info(f"There are {len(categorical_features)} categorical features, which will be binarized.")
        eval_logger.info(f"Missing values for the different attributes are {missing_values_per_feature}.")
        if len(categorical_features) > 0 or sum(missing_values_per_feature) > 0:
            categorical_transformer = Pipeline([
                ("imputer", sklearn.impute.SimpleImputer(strategy="most_frequent")),
                ("binarizer", sklearn.preprocessing.OneHotEncoder(handle_unknown='ignore', sparse = False)),
            ])
            self.mandatory_pre_processing = [("impute_and_binarize", ColumnTransformer(
                transformers=[
                    ("num", numeric_transformer, numeric_features),
                    ("cat", categorical_transformer, categorical_features),
                ]
            ))]
        else:
            self.mandatory_pre_processing = []
    
    def eval_pipeline_on_fold(self, pl, X_train, X_test, y_train, y_test, timeout = None):
        try:
            
            pl = Pipeline(self.mandatory_pre_processing + sklearn.base.clone(pl).steps)
            
            if timeout is None:
                eval_logger.info(f"Fitting model with {X_train.shape[0]} instances and without timeout.")
                pl.fit(X_train, y_train)
            else:
                eval_logger.info(f"Fitting model with {X_train.shape[0]} instances and timeout {timeout}.")
                func_timeout(timeout, pl.fit, (X_train, y_train))
                
            y_hat = pl.predict(X_test)
            error_rate = 1 - sklearn.metrics.accuracy_score(y_test, y_hat)
            eval_logger.info(f"Observed an error rate of {error_rate}")
            return error_rate
        
        except FunctionTimedOut:
            eval_logger.info(f"Timeout observed for evaluation, stopping and returning nan.")
        except KeyboardInterrupt:
            raise
        except:
            eval_logger.info("Observed some exception. Stopping")
        
        return np.nan
    
    def mccv(self, learner, target_size=.9, timeout=None, seed=0, repeats = 10):

        """
        Conducts a 90/10 MCCV (imitating a bit a 10-fold cross validation)
        """
        eval_logger.info(f"Running mccv with seed  {seed}")
        if not timeout is None:
            deadline = time.time() + timeout

        scores = []
        n = self.X.shape[0]
        num_examples = int(target_size * n)
        deadline = None if timeout is None else time.time() + timeout

        seed *= 13
        for r in range(repeats):
            eval_logger.info(f"Seed in MCCV: {seed}. Training on {num_examples} examples. That is {np.round(100 * num_examples / self.X.shape[0])}% of the data (testing on rest).")
            
            # get random train/test split based on seed
            random.seed(seed)
            n = self.X.shape[0]
            indices_train = random.sample(range(n), num_examples)
            mask_train = np.zeros(n)
            mask_train[indices_train] = 1
            mask_train = mask_train.astype(bool)
            mask_test = (1 - mask_train).astype(bool)
            X_train = self.X[mask_train].copy()
            y_train = self.y[mask_train]
            X_test = self.X[mask_test].copy()
            y_test = self.y[mask_test]
            
            # evaluate pipeline
            timeout_local = None if timeout is None else deadline - time.time()
            error_rate = self.eval_pipeline_on_fold(learner, X_train, X_test, y_train, y_test, timeout=timeout_local)
            scores.append(error_rate)
            seed += 1

        return scores
    
    def get_result_of_cv(self, folds, seed = None, timeout = None):
        kf = sklearn.model_selection.KFold(n_splits=folds, random_state=np.random.RandomState(seed), shuffle=True)
        scores = []
        deadline = time.time() + timeout if timeout is not None else None
        for train_index, test_index in kf.split(X):
            X_train, y_train = X[train_index], y[train_index]
            X_test, y_test = X[test_index], y[test_index]
            timeout_loc = None if timeout is None else deadline - time.time()
            error_rate = eval_pipeline_on_fold(learner_inst, X_train, X_test, y_train, y_test, timeout = timeout_loc)
            if not np.isnan(error_rate):
                scores.append(error_rate)
        out = np.mean(scores) if scores else np.nan
        eval_logger.info(f"Returning {out} as the avg over observed scores {scores}")
        return out
    

    def get_pipeline_from_descriptor(self, learner):
        return sklearn.pipeline.Pipeline([(step_name, build_estimator(comp, params, self.X, self.y)) for step_name, (comp, params) in learner])

    '''
        This is the main function that must be implemented by the approaches
    '''
    def select_model(self, learners):
        raise NotImplemented()

        
        
        
        
        

class SH(Evaluator):
    
    def __init__(self, X, y, timeout_per_evaluation, max_train_budget, b_min = 64, seed = 0, repeats = 10):
        self.timeout_per_evaluation = timeout_per_evaluation
        self.b_min = b_min
        self.seed = seed
        self.repeats = repeats
        self.max_train_budget = max_train_budget
        super().__init__(X, y)
    
    def select_model(self, learners):
        b_min = self.b_min
        test_budget = 1 - self.max_train_budget
        b_max = int(self.X.shape[0] * (1 - test_budget))
        timeout = self.timeout_per_evaluation
        print(f"b_max is {b_max}")
        n = len(learners)
        num_phases = int(np.log2(n) - 1)
        eta = (b_max / b_min)**(1/num_phases)
        print(f"Eta is {eta}")
        anchors = [int(np.round(b_min * eta**i)) for i in range(num_phases + 1)]
        populations = [int(np.round(n / (2**i))) for i in range(num_phases + 1)]
        if num_phases != int(num_phases):
            raise Exception(f"Number of learners is {len(learners)}, which is not a power of 2!")
        num_phases = int(num_phases)
        print(f"There will be {num_phases + 1} phases with the following setup.")
        for anchor, population in zip(anchors, populations):
            print(f"Evaluate {population} on {anchor}")

        best_seen_score = np.inf
        best_seen_pl = None

        def get_scores_on_budget(candidates, budget):
            scores = []
            for candidate in tqdm(candidates):

                deadline = None if timeout is None else time.time() + timeout

                temp_pipe = self.get_pipeline_from_descriptor(candidate)
                scores_for_candidate_at_budget = []
                for i in range(self.repeats):
                    if deadline < time.time():
                        break
                    X_train, X_test, y_train, y_test = sklearn.model_selection.train_test_split(self.X, self.y, train_size = budget, test_size = test_budget)
                    error_rate = self.eval_pipeline_on_fold(temp_pipe, X_train, X_test, y_train, y_test, deadline - time.time())
                    if not np.isnan(error_rate):
                        scores_for_candidate_at_budget.append(np.round(error_rate, 4))
                    else:
                        scores_for_candidate_at_budget.append(np.nan)
                scores.append(scores_for_candidate_at_budget)
            return scores

        time_start = time.time()
        population = learners.copy()
        for i, anchor in enumerate(anchors):
            time_start_phase = time.time()
            scores_in_round = get_scores_on_budget(population, anchor)
            runtime_phase = time.time() - time_start_phase
            mean_scores = [np.nanmean(s) for s in scores_in_round]
            index_of_best_mean_score_in_round = np.nanargmin(mean_scores)
            print(index_of_best_mean_score_in_round)
            best_mean_score_in_round = mean_scores[index_of_best_mean_score_in_round]
            if best_mean_score_in_round < best_seen_score:
                best_seen_score = best_mean_score_in_round
                best_seen_pl = population[index_of_best_mean_score_in_round]

            print(f"Finished round {i+1} after {np.round(runtime_phase, 2)}s. Scores are: {mean_scores}.\nBest score was: {best_mean_score_in_round} (all times best score was {best_seen_score})")
            best_indices = np.argsort(mean_scores)[:int(len(population) / 2)]
            print(f"Best indices are: {best_indices}.")
            if len(population) > 2:
                population = [p for j, p in enumerate(population) if j in best_indices]
        runtime = time.time () - time_start

        index_of_winner = np.argmin(mean_scores)
        return self.get_pipeline_from_descriptor(population[index_of_winner])
    

class VerticalEvaluator(Evaluator):
    
    def __init__(self, X, y, validation, timeout_per_evaluation, epsilon, seed=0, exception_on_failure=False):
        super().__init__(X, y)
        if validation == "5cv":
            self.validation_func = lambda pl, seed: self.cv(pl, seed, 5)
        elif validation == "10cv":
            self.validation_func = lambda pl, seed: self.cv(pl, seed, 10)
        elif "lccv" in validation:
            self.r = 1.0
            if validation == "80lccv":
                self.validation_func = self.lccv80            
            elif validation == "80lccv-flex":
                self.validation_func = self.lccv80flex
            elif validation == "90lccv":
                self.validation_func = self.lccv90
            elif validation == "90lccv-flex":
                self.validation_func = self.lccv90flex
            else:
                raise ValueError(f"Unsupported validation function {validation}.")
        else:
            raise ValueError(f"Unsupported validation function {validation}.")
        self.timeout_per_evaluation = timeout_per_evaluation
        self.epsilon = epsilon
        self.seed = seed
        self.exception_on_failure = exception_on_failure
        
    def cv(self, pl, seed, folds):
        kf = sklearn.model_selection.KFold(n_splits=folds, random_state=np.random.RandomState(seed), shuffle=True)
        scores = []
        deadline = time.time() + self.timeout_per_evaluation if self.timeout_per_evaluation is not None else None
        for train_index, test_index in kf.split(self.X):
            learner_inst_copy = sklearn.base.clone(pl)
            X_train, y_train = self.X[train_index], self.y[train_index]
            X_test, y_test = self.X[test_index], self.y[test_index]
            timeout_loc = None if deadline is None else deadline - time.time()
            scores.append(self.eval_pipeline_on_fold(pl, X_train, X_test, y_train, y_test, timeout = timeout_loc))
        out = np.nanmean(scores) if scores else np.nan
        eval_logger.info(f"Returning {out} as the avg over observed scores {scores}")
        return out

    def lccv90(self, pl, seed): # maximum train size is 90% of the data (like for 10CV)
        try:
            enforce_all_anchor_evaluations = self.r == 1
            pl = Pipeline(self.mandatory_pre_processing + pl.steps)
            score = lccv.lccv(pl, self.X, self.y, r=self.r, timeout=self.timeout_per_evaluation, seed=seed, target_anchor=.9, min_evals_for_stability=3, MAX_EVALUATIONS = 10, enforce_all_anchor_evaluations = enforce_all_anchor_evaluations,fix_train_test_folds=True)[0]
            self.r = min(self.r, score)
            return score
        except KeyboardInterrupt:
            raise
        except:
            eval_logger.info("Observed some exception. Returning nan")
            return np.nan

    def lccv80(self, pl, seed=None): # maximum train size is 80% of the data (like for 5CV)
        try:
            enforce_all_anchor_evaluations = self.r == 1
            pl = Pipeline(self.mandatory_pre_processing + pl.steps)
            score = lccv.lccv(pl, self.X, self.y, r=self.r, timeout=self.timeout_per_evaluation, seed=seed, target_anchor=.8, min_evals_for_stability=3, MAX_EVALUATIONS = 5, enforce_all_anchor_evaluations = enforce_all_anchor_evaluations,fix_train_test_folds=True)[0]
            self.r = min(self.r, score)
            return score
        except KeyboardInterrupt:
            raise
        except:
            eval_logger.info("Observed some exception. Returning nan")
            return np.nan

    def lccv90flex(self, pl, seed=None): # maximum train size is 90% of the data (like for 10CV)
        try:
            enforce_all_anchor_evaluations = self.r == 1
            pl = Pipeline(self.mandatory_pre_processing + pl.steps)
            score = lccv.lccv(pl, self.X, self.y, r=self.r, timeout=self.timeout_per_evaluation, seed=seed, target_anchor=.9, enforce_all_anchor_evaluations = enforce_all_anchor_evaluations, fix_train_test_folds=False)[0]
            self.r = min(self.r, score)
            return score
        except KeyboardInterrupt:
            raise
        except Exception as e:
            traceback.print_exc()
            eval_logger.info("Observed some exception. Returning nan")
            return np.nan

    def lccv80flex(self, pl, seed=None): # maximum train size is 80% of the data (like for 5CV)
        try:
            enforce_all_anchor_evaluations = self.r == 1
            pl = Pipeline(self.mandatory_pre_processing + pl.steps)
            score = lccv.lccv(pl, self.X, self.y, r=self.r, timeout=self.timeout_per_evaluation, seed=seed, target_anchor=.8, min_evals_for_stability=3, MAX_EVALUATIONS = 5, enforce_all_anchor_evaluations = enforce_all_anchor_evaluations,fix_train_test_folds=False)[0]
            self.r = min(self.r, score)
            return score
        except KeyboardInterrupt:
            raise
        except:
            eval_logger.info("Observed some exception. Returning nan")
            return np.nan
    
    def select_model(self, learners):
        
        hard_cutoff = 2 * self.timeout_per_evaluation
        r = 1.0
        best_score = 1
        chosen_learner = None
        validation_times = []
        exp_logger = logging.getLogger("experimenter")
        n = len(learners)
        memory_history = []
        index_of_best_learner = -1

        target_anchor = int(np.floor(self.X.shape[0] * .9))  # TODO hardcoded, please fix
        target_anchor_count = 0
        learner_crash_count = 0
        for i, learner in enumerate(learners):
            exp_logger.info(f"""
                --------------------------------------------------
                Checking learner {i + 1}/{n} ({format_learner(learner)})
                --------------------------------------------------""")
            cur_mem = int(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024)
            memory_history.append(cur_mem)
            exp_logger.info(f"Currently used memory: {cur_mem}MB. Memory history is: {memory_history}")
            
            validation_start = time.time()
            temp_pipe = self.get_pipeline_from_descriptor(learner)
            score = self.validation_func(temp_pipe, seed=13 * self.seed + i)
            runtime = time.time() - validation_start
            validation_times.append(runtime)
            eval_logger.info(f"Observed score {score} for {format_learner(temp_pipe)}. Validation took {int(np.round(runtime * 1000))}ms")
            r = min(r, score + self.epsilon)
            eval_logger.info(f"r is now: {r}")
            if score < best_score:
                best_score = score
                chosen_learner = temp_pipe
                index_of_best_learner = i
                eval_logger.info(f"Thas was a NEW BEST score. r has been updated. In other words, currently chosen model is {format_learner(chosen_learner)}")
            else:
                del temp_pipe
                gc.collect()
                eval_logger.info(f"Candidate was NOT competitive. Eliminating the object and garbage collecting.")
        eval_logger.info(f"Chosen learner was found in iteration {index_of_best_learner + 1}")
        return chosen_learner

    
def wilcoxon80(learner, X, y, r = 1.0, seed = None, timeout = None):
    return wilcoxon(learner, X, y, r = r, seed = seed, timeout = timeout, target_size=.8)
    
def wilcoxon(learner, X, y, target_size=.9, r = 0.0, min_stages = 3, timeout=None, seed=0, max_repeats = 10):
    
    def evaluate(learner_inst, X, y, num_examples, seed=0, timeout = None, verbose=False):
        deadline = None if timeout is None else time.time() + timeout
        random.seed(seed)
        n = X.shape[0]
        indices_train = random.sample(range(n), num_examples)
        mask_train = np.zeros(n)
        mask_train[indices_train] = 1
        mask_train = mask_train.astype(bool)
        mask_test = (1 - mask_train).astype(bool)
        X_train = X[mask_train].copy()
        y_train = y[mask_train]
        X_test = X[mask_test].copy()
        y_test = y[mask_test]
        learner_inst = sklearn.base.clone(learner_inst)

        eval_logger.info(f"Training {format_learner(learner_inst)} on data of shape {X_train.shape} using seed {seed}.")
        if deadline is None:
            learner_inst.fit(X_train, y_train)
        else:
            func_timeout(deadline - time.time(), learner_inst.fit, (X_train, y_train))


        y_hat = learner_inst.predict(X_test)
        error_rate = 1 - sklearn.metrics.accuracy_score(y_test, y_hat)
        eval_logger.info(f"Training ready. Obtaining predictions for {X_test.shape[0]} instances. Error rate of model on {len(y_hat)} instances is {error_rate}")
        return error_rate
    
    """
    Conducts a 90/10 MCCV (imitating a bit a 10-fold cross validation)
    """
    eval_logger.info(f"Running mccv with seed  {seed}")
    if not timeout is None:
        deadline = time.time() + timeout
    
    scores = []
    n = X.shape[0]
    num_examples = int(target_size * n)
    
    seed *= 13
    for inner_run in range(max_repeats):
        eval_logger.info(f"Seed in Wilcoxon: {seed}. Training on {num_examples} examples. That is {np.round(100 * num_examples / X.shape[0])}% of the data (testing on rest).")
        if timeout is None:
            try:
                scores.append(evaluate(learner, X, y, num_examples, seed))
            except KeyboardInterrupt:
                raise
                
            except:
                eval_logger.info("AN ERROR OCCURRED, not counting this run!")
        else:
            try:
                if deadline <= time.time():
                    break
                scores.append(func_timeout(deadline - time.time(), evaluate, (learner, X, y, num_examples, seed)))
            except FunctionTimedOut:
                break

            except KeyboardInterrupt:
                raise
                
            except:
                eval_logger.info("AN ERROR OCCURRED, not counting this run!")
            
            # now conduct a wilcoxon signed rank test to determine whether significance has been reached
            scores_currently_best = len(scores) * [r]
            if any(np.array(scores) != np.array(scores_currently_best)):
                statistic, pval = scipy.stats.wilcoxon(scores, scores_currently_best)
                print(pval)
                if pval < 0.05:
                    print(f"reached certainty in fold {inner_run + 1}")
                    break
            else:
                print("omitting test, because all scores are still identical")
        seed += 1

    return np.mean(scores) if len(scores) > 0 else np.nan, scores
    

    
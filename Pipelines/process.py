import pandas as pd
from Pipelines.config_pipelines import sex_list, predictors_wave_1, wave_1_gam, wave_1_gbm, wave_1_rf, TRAIN_PATH, \
    wave_0_0_gam, wave_0_1_gam, predictors_wave_0_0, predictors_wave_0_1, wave_1_cv_rf, wave_2_cvrf_macroeconomic, \
    wave_2_drf_macroeconomic, wave_2_gbm_health, wave_2_gbm_macroeconmic, wave_2_gbm_population, \
    predictors_wave_2_health, predictors_wave_2_macroeconomic, predictors_wave_2_population
import h2o
from h2o.estimators import H2OGradientBoostingEstimator
from h2o.estimators.random_forest import H2ORandomForestEstimator
from h2o.grid.grid_search import H2OGridSearch
from pygam import LinearGAM, GAM, s, f, te, l
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import GridSearchCV
from sklearn.impute import SimpleImputer


def process_raw(path: str):
    df = pd.read_csv(path)
    return df


def take_difference(df: pd.DataFrame, sex_list=sex_list):

    df = df.rename(columns={
        'Intergalactic Development Index (IDI), female, Rank': 'Intergalactic Development Index (IDI), Rank, female',
        'Intergalactic Development Index (IDI), male, Rank': 'Intergalactic Development Index (IDI), Rank, male'})
    for i in range(0, len(sex_list), 2):
        label = sex_list[i].split(', male')[0]
        df.loc[:, 'diff - {0}'.format(label)] = df.loc[:, sex_list[i]] - df.loc[:, sex_list[i + 1]]

    df = df.drop(columns=sex_list)

    return df


def take_population_rates(df: pd.DataFrame):

    df.loc[:, 'Population, ages 15–64 (rate)'] = \
        df['Population, ages 15–64 (millions)']/df['Population, total (millions)']
    df.loc[:, 'Population, ages 65 and older (rate)'] = \
        df['Population, ages 65 and older (millions)']/df['Population, total (millions)']
    df.loc[:, 'Population, under age 5 (rate)'] = \
        df['Population, under age 5 (millions)']/df['Population, total (millions)']

    df = df.drop(columns=['Population, ages 15–64 (millions)', 'Population, ages 65 and older (millions)',
                          'Population, under age 5 (millions)'])

    return df


def loose_correlated_vars(df: pd.DataFrame, df_train, threshold=0.75,
                          manual=['Education Index', 'Intergalactic Development Index (IDI)']):

    corr = df_train.loc[:, (df_train.columns != 'galactic year')
                           & (df_train.columns != 'galaxy') & (df_train.columns != 'y')].corr()

    clean = corr.columns[[sum(df_train[col].isna())/len(df_train) < 0.15 for col in corr.columns]]
    unclean = corr.columns[[sum(df_train[col].isna())/len(df_train) >= 0.15 for col in corr.columns]]
    to_remove = abs(corr.loc[:, clean]) > threshold
    to_remove = to_remove.loc[unclean]
    to_remove['to keep'] = [sum(to_remove.iloc[i]) < 2 for i in range(len(to_remove))]

    df = df.drop(columns=to_remove[~to_remove['to keep']].index)
    df = df.drop(columns=manual)

    df_train = df_train.drop(columns=to_remove[~to_remove['to keep']].index)
    df_train = df_train.drop(columns=manual)

    return df, df_train


def h2o_gbm(df: pd.DataFrame, targets: list, predictors: list, df_train: pd.DataFrame):

    df_train_return = df_train.copy()

    tmp_df_to_predict = df.dropna(subset=predictors).copy()
    tmp_df_to_change_train = df_train_return.dropna(subset=predictors).copy()

    for TARGET in targets:

        tmp_df_to_train_on = df_train.loc[:, predictors + [TARGET]]

        index_to_predict_test = np.array(tmp_df_to_predict[TARGET].isna())
        index_to_predict_train = np.array(tmp_df_to_change_train[TARGET].isna())

        test_df = tmp_df_to_predict.loc[index_to_predict_test, predictors]
        change_train_df = tmp_df_to_change_train.loc[index_to_predict_train, predictors]

        tmp_df_to_train_on = tmp_df_to_train_on.dropna()

        train = h2o.H2OFrame(tmp_df_to_train_on)

        test = h2o.H2OFrame(test_df)
        change_train = h2o.H2OFrame(change_train_df)

        response = TARGET

        hyper_params_tune = {'max_depth': list(range(1, 21, 1)),
                             'sample_rate': [x / 100. for x in range(20, 101)],
                             'col_sample_rate': [x / 100. for x in range(20, 101)],
                             'col_sample_rate_per_tree': [x / 100. for x in range(20, 101)],
                             'col_sample_rate_change_per_level': [x / 100. for x in range(90, 111)],
                             'min_rows': [1, 2, 4, 8, 16, 25],
                             'nbins': [2 ** x for x in range(4, 9)],
                             'nbins_cats': [2 ** x for x in range(4, 9)],
                             'min_split_improvement': [0, 1e-8, 1e-6, 1e-4],
                             'histogram_type': ["UniformAdaptive", "QuantilesGlobal", "RoundRobin"]}

        search_criteria_tune = {'strategy': "RandomDiscrete",
                                'max_runtime_secs': 3600,  ## limit the runtime to 60 minutes
                                'max_models': 100,  ## build no more than 100 models
                                'seed': 1234,
                                'stopping_rounds': 5,
                                'stopping_metric': "rmse",
                                'stopping_tolerance': 1e-3
                                }

        h2ogbm = H2OGradientBoostingEstimator(nfolds=5,
                                              learn_rate=0.05,
                                              learn_rate_annealing=0.99,
                                              score_tree_interval=10,
                                              stopping_rounds=5,
                                              stopping_metric="rmse",
                                              stopping_tolerance=1e-4,
                                              ntrees=1000,
                                              seed=1111,
                                              keep_cross_validation_predictions=True,
                                              distribution='gaussian')

        h2ogbm = H2OGridSearch(h2ogbm, grid_id='gbm_.{0}'.format(TARGET), hyper_params=hyper_params_tune,
                               search_criteria=search_criteria_tune)

        h2ogbm.train(x=predictors, y=response, training_frame=train, seed=1111)

        gbm_gridperf1 = h2ogbm.get_grid(sort_by='rmse', decreasing=False)

        bestgbm = gbm_gridperf1.models[0]

        pred = h2o.as_list(bestgbm.predict(test.drop(len(predictors))), use_pandas=True)
        train_imput = h2o.as_list(bestgbm.predict(change_train.drop(len(predictors))), use_pandas=True)

        tmp_df_to_predict.loc[index_to_predict_test, TARGET] = pred.values
        tmp_df_to_change_train.loc[index_to_predict_train, TARGET] = train_imput.values

        df = df.set_index(['galaxy', 'galactic year']).fillna(
                tmp_df_to_predict.set_index(['galaxy', 'galactic year'])).reset_index()

        df_train_return = df_train_return.set_index(['galaxy', 'galactic year']).fillna(
            tmp_df_to_change_train.set_index(['galaxy', 'galactic year'])).reset_index()

        h2o.remove_all()

    return df, df_train_return


def h2o_drf(df: pd.DataFrame, targets: list, predictors: list, df_train: pd.DataFrame):

    df_train_return = df_train.copy()

    tmp_df_to_predict = df.dropna(subset=predictors).copy()
    tmp_df_to_change_train = df_train_return.dropna(subset=predictors).copy()

    for TARGET in targets:
        tmp_df_to_train_on = df_train.loc[:, predictors + [TARGET]]

        index_to_predict_test = np.array(tmp_df_to_predict[TARGET].isna())
        index_to_predict_train = np.array(tmp_df_to_change_train[TARGET].isna())

        test_df = tmp_df_to_predict.loc[index_to_predict_test, predictors]
        change_train_df = tmp_df_to_change_train.loc[index_to_predict_train, predictors]

        tmp_df_to_train_on = tmp_df_to_train_on.dropna()

        train = h2o.H2OFrame(tmp_df_to_train_on)

        test = h2o.H2OFrame(test_df)
        change_train = h2o.H2OFrame(change_train_df)

        response = TARGET

        hyper_params_tune = {
            'max_depth': list(range(5, 60, 1)),
            'nbins': [2 ** x for x in range(4, 9)],
            'nbins_cats': [2 ** x for x in range(4, 9)],
            'ntrees': [100, 200, 400, 800, 1000],
            'histogram_type': ["UniformAdaptive", "QuantilesGlobal", "RoundRobin"]}

        search_criteria_tune = {'strategy': "RandomDiscrete",
                                'max_runtime_secs': 3600,  ## limit the runtime to 60 minutes
                                'max_models': 100,  ## build no more than 100 models
                                'seed': 1234,
                                'stopping_rounds': 5,
                                'stopping_metric': "rmse",
                                'stopping_tolerance': 1e-3
                                }

        rf_v1 = H2ORandomForestEstimator(model_id="rf_covType_v1")

        h2orf = H2OGridSearch(rf_v1, grid_id='gbm_.{0}'.format(TARGET), hyper_params=hyper_params_tune,
                              search_criteria=search_criteria_tune)

        h2orf.train(x=predictors, y=response, training_frame=train, seed=1111)

        rf_gridperf1 = h2orf.get_grid(sort_by='mse', decreasing=False)

        bestrf = rf_gridperf1.models[0]

        pred = h2o.as_list(bestrf.predict(test.drop(len(predictors))), use_pandas=True)
        train_imput = h2o.as_list(bestrf.predict(change_train.drop(len(predictors))), use_pandas=True)

        tmp_df_to_predict.loc[index_to_predict_test, TARGET] = pred.values
        tmp_df_to_change_train.loc[index_to_predict_train, TARGET] = train_imput.values

        df = df.set_index(['galaxy', 'galactic year']).fillna(
                tmp_df_to_predict.set_index(['galaxy', 'galactic year'])).reset_index()

        df_train_return = df_train_return.set_index(['galaxy', 'galactic year']).fillna(
            tmp_df_to_change_train.set_index(['galaxy', 'galactic year'])).reset_index()

        h2o.remove_all()

    return df, df_train_return


def gam_wave_0(df, df_train):

    df_train_return = df_train.copy()

    galaxy_to_int = dict((i, g) for g, i in enumerate(df_train.galaxy.unique()))
    int_to_galaxy = {v: k for k, v in galaxy_to_int.items()}
    df_train.loc[:, ['galaxy']] = [galaxy_to_int[i] for i in df_train.galaxy]

    tmp_df_to_change_train = df_train_return.loc[:, predictors_wave_0_0].dropna()
    tmp_df_to_predict = df.loc[:, predictors_wave_0_0].dropna()

    for TARGET in wave_0_0_gam:
        tmp_df_to_train_on = df_train.loc[:, predictors_wave_0_0 + [TARGET]].dropna()

        X = tmp_df_to_train_on.loc[:, tmp_df_to_train_on.columns != TARGET]
        y = tmp_df_to_train_on.loc[:, TARGET]

        lams = np.exp(np.random.random(size=(500, 6)) * 6 - 3)

        gam = LinearGAM(s(0, dtype='categorical', by=1) + f(0) + s(1) + s(2) + s(3) + s(4)).gridsearch(
            np.array(X), np.array(y), lam=lams)

        tmp_df_to_predict.loc[:, ['galaxy']] = [galaxy_to_int[i] for i in tmp_df_to_predict.galaxy]
        tmp_df_to_change_train.loc[:, ['galaxy']] = [galaxy_to_int[i] for i in tmp_df_to_change_train.galaxy]

        tmp_df_to_predict[TARGET] = gam.predict(tmp_df_to_predict)
        tmp_df_to_change_train[TARGET] = gam.predict(tmp_df_to_change_train)

        tmp_df_to_predict.loc[:, ['galaxy']] = [int_to_galaxy[i] for i in tmp_df_to_predict.galaxy]
        tmp_df_to_change_train.loc[:, ['galaxy']] = [int_to_galaxy[i] for i in tmp_df_to_change_train.galaxy]

        df = df.set_index(['galaxy', 'galactic year']).fillna(
                tmp_df_to_predict.set_index(['galaxy', 'galactic year'])).reset_index()

        df_train_return = df_train_return.set_index(['galaxy', 'galactic year']).fillna(
                tmp_df_to_change_train.set_index(['galaxy', 'galactic year'])).reset_index()

        tmp_df_to_predict = tmp_df_to_predict.drop(columns = [TARGET], axis =1)
        tmp_df_to_change_train = tmp_df_to_change_train.drop(columns=[TARGET], axis=1)

    tmp_df_to_predict = df.loc[:, predictors_wave_0_1].dropna()
    tmp_df_to_change_train = df_train_return.loc[:, predictors_wave_0_1].dropna()

    for TARGET in wave_0_1_gam:
        tmp_df_to_train_on = df_train.loc[:, predictors_wave_0_1 + [TARGET]].dropna()

        X = tmp_df_to_train_on.loc[:, tmp_df_to_train_on.columns != TARGET]
        y = tmp_df_to_train_on.loc[:, TARGET]

        lams = np.exp(np.random.random(size=(500, 7)) * 6 - 3)

        gam = LinearGAM(s(0, dtype='categorical', by=1) + f(0) + s(1) + s(2) + s(3) + s(4) + s(5)).gridsearch(
            np.array(X), np.array(y), lam=lams)

        tmp_df_to_predict.loc[:, ['galaxy']] = [galaxy_to_int[i] for i in tmp_df_to_predict.galaxy]
        tmp_df_to_change_train.loc[:, ['galaxy']] = [galaxy_to_int[i] for i in tmp_df_to_change_train.galaxy]

        tmp_df_to_predict[TARGET] = gam.predict(tmp_df_to_predict)
        tmp_df_to_change_train[TARGET] = gam.predict(tmp_df_to_change_train)

        tmp_df_to_predict.loc[:, ['galaxy']] = [int_to_galaxy[i] for i in tmp_df_to_predict.galaxy]
        tmp_df_to_change_train.loc[:, ['galaxy']] = [int_to_galaxy[i] for i in tmp_df_to_change_train.galaxy]

        df = df.set_index(['galaxy', 'galactic year']).fillna(
                tmp_df_to_predict.set_index(['galaxy', 'galactic year'])).reset_index()
        df_train_return = df_train_return.set_index(['galaxy', 'galactic year']).fillna(
            tmp_df_to_change_train.set_index(['galaxy', 'galactic year'])).reset_index()

        tmp_df_to_predict = tmp_df_to_predict.drop(columns=[TARGET], axis=1)
        tmp_df_to_change_train = tmp_df_to_change_train.drop(columns=[TARGET], axis=1)

    return df, df_train_return


def gam_wave_1(df, df_train: pd.DataFrame):

    galaxy_to_int = dict((i, g) for g, i in enumerate(df_train.galaxy.unique()))
    int_to_galaxy = {v: k for k, v in galaxy_to_int.items()}

    tmp_df_to_predict = df.loc[:, predictors_wave_1].dropna()

    tmp_df_to_change_train = df_train.loc[:, predictors_wave_1].dropna()

    df_train_return = df_train.copy()

    df_train.loc[:, ['galaxy']] = [galaxy_to_int[i] for i in df_train.galaxy]

    for TARGET in wave_1_gam:
        tmp_df_to_train_on = df_train.loc[:, predictors_wave_1 + [TARGET]]

        tmp_df_to_train_on = tmp_df_to_train_on.dropna()

        X = tmp_df_to_train_on.loc[:, tmp_df_to_train_on.columns != TARGET]
        y = tmp_df_to_train_on.loc[:, TARGET]

        lams = np.exp(np.random.random(size=(500, 9)) * 6 - 3)

        gam = LinearGAM(s(0, dtype='categorical', by=1) + f(0) + s(1) + s(2) + s(3) + s(4) + s(5) + s(6) + s(7)).gridsearch(
            np.array(X), np.array(y), lam=lams)

        tmp_df_to_predict.loc[:, ['galaxy']] = [galaxy_to_int[i] for i in tmp_df_to_predict.galaxy]
        tmp_df_to_change_train.loc[:,['galaxy']] = [galaxy_to_int[i] for i in tmp_df_to_change_train.galaxy]

        tmp_df_to_predict[TARGET] = gam.predict(tmp_df_to_predict)
        tmp_df_to_change_train[TARGET] = gam.predict(tmp_df_to_change_train)

        tmp_df_to_predict.loc[:, ['galaxy']] = [int_to_galaxy[i] for i in tmp_df_to_predict.galaxy]
        tmp_df_to_change_train.loc[:, ['galaxy']] = [int_to_galaxy[i] for i in tmp_df_to_change_train.galaxy]

        df = df.set_index(['galaxy', 'galactic year']).fillna(
                tmp_df_to_predict.set_index(['galaxy', 'galactic year'])).reset_index()

        df_train_return = df_train_return.set_index(['galaxy', 'galactic year']).fillna(
                tmp_df_to_change_train.set_index(['galaxy', 'galactic year'])).reset_index()

        tmp_df_to_predict = tmp_df_to_predict.drop(columns=[TARGET], axis=1)
        tmp_df_to_change_train = tmp_df_to_change_train.drop(columns=[TARGET], axis=1)

    return df, df_train_return


def random_forest(df, targets_to_grids, predictors, df_train):

    possible_galaxies = list(df_train.galaxy.unique())

    df_return = df.copy()
    df = df.loc[:, predictors]
    df.galaxy = df.galaxy.astype(pd.CategoricalDtype(categories=possible_galaxies))
    df = pd.get_dummies(df)
    df['galaxy'] = df_return['galaxy']

    df_train_return = df_train.copy()
    df_train = df_train.loc[:, predictors]
    df_train = pd.get_dummies(df_train)
    df_train['galaxy'] = df_train_return['galaxy']

    for TARGET in targets_to_grids.keys():

        df_train[TARGET] = df_train_return[TARGET]
        df[TARGET] = df_return[TARGET]

        tmp_df_to_predict = df.dropna(subset=predictors).copy()
        tmp_df_to_change_train = df_train.dropna(subset=predictors).copy()

        index_to_predict_test = np.array(tmp_df_to_predict[TARGET].isna())
        index_to_predict_train = np.array(tmp_df_to_change_train[TARGET].isna())

        test_df = tmp_df_to_predict.loc[index_to_predict_test,
                                                     ((tmp_df_to_predict.columns != TARGET)
                                                      & (tmp_df_to_predict.columns != 'galaxy'))]

        change_train_df = tmp_df_to_change_train.loc[index_to_predict_train,
                                                          ((tmp_df_to_change_train.columns != TARGET)
                                                           & (tmp_df_to_change_train.columns != 'galaxy'))]

        tmp_df_to_train_on = df_train.dropna()

        param_grid = targets_to_grids[TARGET]

        X, y = tmp_df_to_train_on.loc[:, ((tmp_df_to_train_on.columns != TARGET)
                                          & (tmp_df_to_train_on.columns != 'galaxy'))], tmp_df_to_train_on[TARGET]

        regr = RandomForestRegressor(random_state=0)

        clf = GridSearchCV(regr, param_grid)
        clf.fit(X, y)

        tmp_df_to_predict.loc[index_to_predict_test, TARGET] = clf.predict(test_df)

        tmp_df_to_change_train.loc[index_to_predict_train,TARGET] = clf.predict(change_train_df)

        df_return = df_return.set_index(['galaxy', 'galactic year']).fillna(
                tmp_df_to_predict.set_index(['galaxy', 'galactic year'])).reset_index()

        df_train_return = df_train_return.set_index(['galaxy', 'galactic year']).fillna(
                tmp_df_to_change_train.set_index(['galaxy', 'galactic year'])).reset_index()

        df_train = df_train.drop(columns=[TARGET], axis=1)
        df = df.drop(columns=[TARGET], axis=1)

    return df_return, df_train_return


def last_imputation(df, df_train):
    
    for galaxy in df_train.galaxy.unique():
        index_train = df_train.galaxy == galaxy
        index_test = df.galaxy == galaxy
        for column in df_train.columns:
            if (column != 'galaxy') & (column != 'y'):
                df.loc[index_test, column] = df.loc[index_test, column].fillna(df_train.loc[index_train,column].mean())
                
                
    imp = SimpleImputer(missing_values=np.nan, strategy='mean').fit(df_train.loc[:, (df_train.columns != 'galaxy') & (df_train.columns != 'y')])
    
    tmp = pd.DataFrame(imp.transform(df.loc[:, (df.columns != 'galaxy') & (df.columns != 'y')]))
    tmp.columns = df.loc[:, (df.columns != 'galaxy') & (df.columns != 'y')].columns
    tmp.index = df.loc[:, (df.columns != 'galaxy') & (df.columns != 'y')].index
    df.loc[:, (df.columns != 'galaxy') & (df.columns != 'y')] = tmp
    
    return df

def imputation_waves(df: pd.DataFrame):

    df_train_0 = process_raw(TRAIN_PATH).pipe(take_difference).pipe(take_population_rates)

    h2o.init(nthreads=-1,min_mem_size='5G', max_mem_size='10G')

    df, df_train_1 = loose_correlated_vars(df, df_train_0)
    df, df_train_2 = df.pipe(gam_wave_0, df_train_1)
    df, df_train_3 = h2o_gbm(df, wave_1_gbm, predictors_wave_1, df_train=df_train_2)
    df, df_train_4 = h2o_drf(df, wave_1_rf, predictors_wave_1, df_train=df_train_3)
    df, df_train_5 = gam_wave_1(df, df_train=df_train_4)
    df, df_train_6 = random_forest(df, wave_1_cv_rf, predictors_wave_1, df_train=df_train_5)
    df, df_train_7 = h2o_gbm(df, wave_2_gbm_population, predictors_wave_2_population, df_train = df_train_6)
    df, df_train_8 = h2o_gbm(df, wave_2_gbm_macroeconmic, predictors_wave_2_macroeconomic, df_train = df_train_7)
    df, df_train_9 = h2o_gbm(df, wave_2_gbm_health, predictors_wave_2_health, df_train = df_train_8)
    df, df_train_10 = h2o_drf(df, wave_2_drf_macroeconomic, predictors_wave_2_macroeconomic, df_train = df_train_9)
    df, df_train_11 = random_forest(df, wave_2_cvrf_macroeconomic, predictors_wave_2_macroeconomic, df_train = df_train_10)
    df = df.pipe(last_imputation, df_train_11)
    df_train_11 = df_train_11.pipe(last_imputation, df_train_11)

    h2o.shutdown()

    return df, df_train_11

import csv
import pandas as pd
import numpy as np
import sqlite3
import os
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
from datetime import datetime, timedelta
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import dash
from dash import dcc, html, Input, Output, callback_context
import dash_bootstrap_components as dbc
from dash import dash_table
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.linear_model import LinearRegression, Ridge, Lasso
from sklearn.ensemble import RandomForestRegressor
from sklearn.preprocessing import StandardScaler
from statsmodels.tsa.arima.model import ARIMA
import xgboost as xgb

table = pd.read_csv('data/SKUFormula 2.csv')
print(table.head())
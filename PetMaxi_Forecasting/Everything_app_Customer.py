#!/usr/bin/env python3
"""
Monte Carlo Forecasting Application
Simple Monte Carlo simulation with seasonality and peak detection
Focused on MAE, MAPE, and RMSE metrics
"""

import csv
import traceback
from duckdb import df
from flask import ctx
import pandas as pd
import numpy as np
import sqlite3
import os
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
from dateutil.relativedelta import relativedelta
from datetime import datetime, timedelta
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import dash
from dash import State, dcc, html, Input, Output, callback_context
import dash_bootstrap_components as dbc
from dash import dash_table
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.linear_model import LinearRegression, Ridge, Lasso
from sklearn.ensemble import RandomForestRegressor
from sklearn.preprocessing import StandardScaler
from statsmodels.tsa.arima.model import ARIMA
import xgboost as xgb

#---------------------------------NEW FEATURE--------------------#
TRAINING_WINDOWS = [
    {'label': 'Last 3 Months', 'value': '3m', 'months': 3},
    {'label': 'Last 6 Months', 'value': '6m', 'months': 6},
    {'label': 'Last 12 Months', 'value': '12m', 'months': 12},
]
#----------------------------------------------------------------#
class MonteCarloForecaster:
    """Monte Carlo simulation-based forecasting with seasonality and peaks"""
    
    def __init__(self, db_path="monte_carlo_forecasting.db", fresh_start=True):
        self.db_path = db_path
        self.fresh_start = fresh_start
        
        # Create fresh database if requested
        if self.fresh_start:
            self.create_fresh_database()
        
        # Initialize Dash app
        self.app = dash.Dash(
           __name__,
           external_stylesheets=[dbc.themes.BOOTSTRAP],
           requests_pathname_prefix="/petmaxi-dashboard/",
           routes_pathname_prefix="/petmaxi-dashboard/"
        )
        
        # Store latest report for CSV export
        self.latest_report = None
        
    def create_fresh_database(self):
        """Create a fresh database by removing the old one"""
        try:
            if os.path.exists(self.db_path):
                print(f"🗑️  Removing old database: {self.db_path}")
                os.remove(self.db_path)
                print("✅ Old database removed")
            
            # Create new database
            conn = sqlite3.connect(self.db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.close()
            print("✅ Fresh database created")
            
        except Exception as e:
            print(f"⚠️  Could not create fresh database: {e}")
    
    def get_db_connection(self):
        """Get a database connection"""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn
    
    def prepare_features_for_ml_models(self, data, target_col='qtton'):
        """Prepare features for machine learning models with fallback for insufficient data"""
        try:
            if data.empty or target_col not in data.columns:
                return None, None, None
            
            # Ensure date column exists and is datetime
            if 'date_short' not in data.columns:
                return None, None, None
            
            data['date_short'] = pd.to_datetime(data['date_short'], errors='coerce')
            data = data.dropna(subset=['date_short', target_col])
            data = data.sort_values('date_short')
            
            if len(data) < 5:
                return None, None, None
            
            # Create basic features
            data['day_of_year'] = data['date_short'].dt.dayofyear
            data['day_of_week'] = data['date_short'].dt.dayofweek
            data['month'] = data['date_short'].dt.month
            data['trend'] = range(len(data))
            
            # Try to add lag features if we have enough data
            if len(data) >= 14:
                for lag in [1, 2, 3, 7]:
                    data[f'lag_{lag}'] = data[target_col].shift(lag)
                
                data_with_lags = data.dropna()
                
                if len(data_with_lags) >= 5:
                    # Use full feature set with lag features
                    feature_cols = ['day_of_year', 'day_of_week', 'month', 'trend', 'lag_1', 'lag_2', 'lag_3', 'lag_7']
                    return data_with_lags, feature_cols, data_with_lags[target_col].tail(7).tolist()
            
            # Fallback to basic features without lag features
            feature_cols = ['day_of_year', 'day_of_week', 'month', 'trend']
            return data, feature_cols, data[target_col].tail(7).tolist()
            
        except Exception as e:
            print(f"Error preparing features: {e}")
            return None, None, None
    
    def load_csv_data(self):
        """Load all CSV files automatically"""
        print("🔍 LOADING CSV DATA FOR MONTE CARLO...")
        print("=" * 50)
        
        csv_files = [
            os.path.join(DATA_DIR, f)
            for f in os.listdir(DATA_DIR)
            if f.endswith(".csv")
        ]
        
        if not csv_files:
            print("❌ No CSV files found")
            return False
        
        print(f"📊 Found {len(csv_files)} CSV files: {csv_files}")
        
        conn = self.get_db_connection()
        try:
            loaded_tables = {}
            
            for csv_file in csv_files:
                print(f"\n📈 Processing {csv_file}...")
                
                try:
                    # Read CSV
                    df = pd.read_csv(csv_file)
                    print(f"  📊 Loaded {len(df):,} records")
                    
                    # Clean column names
                    df.columns = df.columns.str.strip().str.lower().str.replace(' ', '_')
                    
                    # Create table name
                    table_name = os.path.basename(csv_file)\
                        .lower()\
                        .replace(".csv", "")\
                        .replace(" ", "_")
                    
                    # Handle date columns
                    date_cols = [col for col in df.columns if 'date' in col.lower()]
                    for col in date_cols:
                        df[col] = pd.to_datetime(df[col], errors='coerce')
                        print(f"  📅 Converted {col} to datetime")
                    
                    # Handle numeric columns
                    numeric_cols = ['qtton', 'qtsac', 'weight', 'ton', 'year', 'month', 'day']
                    for col in numeric_cols:
                        if col in df.columns:
                            df[col] = pd.to_numeric(df[col], errors='coerce')
                            print(f"  🔢 Converted {col} to numeric")
                    
                    # Save to database
                    conn.execute(f"DROP TABLE IF EXISTS {table_name}")
                    df.to_sql(table_name, conn, index=False)
                    
                    count = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                    print(f"  ✅ Saved {count:,} records to '{table_name}'")
                    loaded_tables[table_name] = count
                    
                except Exception as e:
                    print(f"  ❌ Error processing {csv_file}: {e}")
                    continue
            
            conn.commit()
            
            # Show summary
            print(f"\n🎉 CSV DATA LOADING COMPLETE!")
            print("=" * 50)
            print("📊 LOADED TABLES:")
            for table, count in loaded_tables.items():
                print(f"  {table}: {count:,} records")
            
            # Check for SKUs
            try:
                sku_count = conn.execute("SELECT COUNT(DISTINCT sku) FROM sales_data WHERE sku IS NOT NULL").fetchone()[0]
                print(f"\n🎯 Found {sku_count} unique SKUs in sales data")
            except:
                print("\n⚠️  Could not count SKUs - check sales data format")
            
            return True
            
        except Exception as e:
            print(f"❌ Error loading CSV: {e}")
            return False
        finally:
            conn.close()
    
    def get_available_skus(self, min_records=10):
        """Get available SKUs"""
        try:
            conn = self.get_db_connection()
            
            # First try standard table names
            table_names = ['%sales%'] #HARDCODED 'sales_data', 'salesdata', 'sales','Sales_Data_1yr_1']
            for table_name in table_names:
                try:
                    query = f"""
                    SELECT sku, COUNT(*) as count 
                    FROM {table_name} 
                    WHERE sku IS NOT NULL AND sku != ''
                    GROUP BY sku 
                    HAVING COUNT(*) >= {min_records}
                    ORDER BY COUNT(*) DESC
                    """
                    df = pd.read_sql_query(query, conn)
                    if not df.empty:
                        skus = df['sku'].tolist()
                        conn.close()
                        print(f"📊 Found {len(skus)} SKUs with sufficient data")
                        return skus
                except:
                    continue
            
            # If no standard tables found, look for any table with 'sales' in the name
            try:
                tables_query = "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%sales%'"
                tables_df = pd.read_sql_query(tables_query, conn)
                if not tables_df.empty:
                    for table_name in tables_df['name'].tolist():
                        try:
                            query = f"""
                            SELECT sku, COUNT(*) as count 
                            FROM {table_name} 
                            WHERE sku IS NOT NULL AND sku != ''
                            GROUP BY sku 
                            HAVING COUNT(*) >= {min_records}
                            ORDER BY COUNT(*) DESC
                            LIMIT 100
                            """
                            df = pd.read_sql_query(query, conn)
                            if not df.empty:
                                skus = df['sku'].tolist()
                                conn.close()
                                print(f"📊 Found {len(skus)} SKUs with sufficient data in {table_name}")
                                return skus
                        except:
                            continue
            except:
                pass
            
            conn.close()
            return []
            
        except Exception as e:
            print(f"❌ Error getting SKUs: {e}")
            return []
    
    def get_available_families(self, min_records=10):
        """Get available families"""
        try:
            conn = self.get_db_connection()
            
            # First try standard table names
            table_names = ['sales_data', 'salesdata', 'sales','%salesdata%']
            for table_name in table_names:
                try:
                    query = f"""
                    SELECT family, COUNT(*) as count 
                    FROM {table_name} 
                    WHERE family IS NOT NULL AND family != ''
                    GROUP BY family 
                    HAVING COUNT(*) >= {min_records}
                    ORDER BY COUNT(*) DESC
                    """
                    df = pd.read_sql_query(query, conn)
                    if not df.empty:
                        families = df['family'].tolist()
                        conn.close()
                        print(f"📊 Found {len(families)} families with sufficient data")
                        return families
                except:
                    continue
            
            # If no standard tables found, look for any table with 'sales' in the name
            try:
                tables_query = "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%sales%'"
                tables_df = pd.read_sql_query(tables_query, conn)
                if not tables_df.empty:
                    for table_name in tables_df['name'].tolist():
                        try:
                            query = f"""
                            SELECT family, COUNT(*) as count 
                            FROM {table_name} 
                            WHERE family IS NOT NULL AND family != ''
                            GROUP BY family 
                            HAVING COUNT(*) >= {min_records}
                            ORDER BY COUNT(*) DESC
                            LIMIT 100
                            """
                            df = pd.read_sql_query(query, conn)
                            if not df.empty:
                                families = df['family'].tolist()
                                conn.close()
                                print(f"📊 Found {len(families)} families with sufficient data in {table_name}")
                                return families
                        except:
                            continue
            except:
                pass
            
            conn.close()
            return []
            
        except Exception as e:
            print(f"❌ Error getting families: {e}")
            return []
    
    def get_sku_data(self, sku, limit=10000):
        """Get SKU data for analysis"""
        try:
            conn = self.get_db_connection()
            
            # First try standard table names
            table_names = ['sales_data', 'salesdata', 'sales','%salesdata%']
            for table_name in table_names:
                try:
                    query = f"""
                    SELECT * FROM {table_name} 
                    WHERE sku = ? 
                    ORDER BY date_short DESC
                    LIMIT {limit}
                    """
                    df = pd.read_sql_query(query, conn, params=[sku])
                    if not df.empty:
                        conn.close()
                        return df
                except:
                    continue
            
            # If no standard tables found, look for any table with 'sales' in the name
            try:
                tables_query = "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%sales%'"
                tables_df = pd.read_sql_query(tables_query, conn)
                if not tables_df.empty:
                    for table_name in tables_df['name'].tolist():
                        try:
                            query = f"""
                            SELECT * FROM {table_name} 
                            WHERE sku = ? 
                            ORDER BY date_short DESC
                            LIMIT {limit}
                            """
                            df = pd.read_sql_query(query, conn, params=[sku])
                            if not df.empty:
                                conn.close()
                                return df
                        except:
                            continue
            except:
                pass
            
            conn.close()
            return pd.DataFrame()
            
        except Exception as e:
            print(f"❌ Error getting SKU data: {e}")
            return pd.DataFrame()
    
    def get_family_data(self, family, limit=10000):
        """Get family data for analysis"""
        try:
            conn = self.get_db_connection()
            
            # First try standard table names
            table_names = ['sales_data', 'salesdata', 'sales','%salesdata%']
            for table_name in table_names:
                try:
                    query = f"""
                    SELECT * FROM {table_name} 
                    WHERE family = ? 
                    ORDER BY date_short DESC
                    LIMIT {limit}
                    """
                    df = pd.read_sql_query(query, conn, params=[family])
                    if not df.empty:
                        conn.close()
                        return df
                except:
                    continue
            
            # If no standard tables found, look for any table with 'sales' in the name
            try:
                tables_query = "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%sales%'"
                tables_df = pd.read_sql_query(tables_query, conn)
                if not tables_df.empty:
                    for table_name in tables_df['name'].tolist():
                        try:
                            query = f"""
                            SELECT * FROM {table_name} 
                            WHERE family = ? 
                            ORDER BY date_short DESC
                            LIMIT {limit}
                            """
                            df = pd.read_sql_query(query, conn, params=[family])
                            if not df.empty:
                                conn.close()
                                return df
                        except:
                            continue
            except:
                pass
            
            conn.close()
            return pd.DataFrame()
            
        except Exception as e:
            print(f"❌ Error getting family data: {e}")
            return pd.DataFrame()
    
    def aggregate_family_data(self, family_data, target_col='qtton'):
        """Aggregate family data by date"""
        if family_data.empty or target_col not in family_data.columns:
            return pd.DataFrame()
        
        # Ensure date column exists and is datetime
        if 'date_short' not in family_data.columns:
            return pd.DataFrame()
        
        family_data['date_short'] = pd.to_datetime(family_data['date_short'], errors='coerce')
        family_data = family_data.dropna(subset=['date_short', target_col])
        
        if family_data.empty:
            return pd.DataFrame()
        
        # Aggregate by date (sum all SKUs in family for each date)
        aggregated = family_data.groupby('date_short')[target_col].sum().reset_index()
        aggregated['family'] = family_data['family'].iloc[0] if 'family' in family_data.columns else 'Unknown'
        
        return aggregated
    
#--------- NEW FEATURE: Filter by training window --------------#    
    def filter_by_training_window(self, df, months):
        if df.empty or 'date_short' not in df.columns:
            return pd.DataFrame()
        df = df.copy()
        df['date_short'] = pd.to_datetime(df['date_short'], errors='coerce')
        df = df.dropna(subset=['date_short']).sort_values('date_short')
        max_date = df['date_short'].max()
        min_date = max_date - relativedelta(months=months)
        return df[df['date_short'] >= min_date]
    
#---------------------------------------------------------------#   
    def analyze_seasonality(self, df, target_col='qtton'):
        """Analyze seasonality patterns"""
        if df.empty or target_col not in df.columns:
            return {}
        
        # Ensure date column exists and is datetime
        if 'date_short' not in df.columns:
            return {}
        
        df['date_short'] = pd.to_datetime(df['date_short'], errors='coerce')
        df = df.dropna(subset=['date_short', target_col])
        
        if len(df) < 30:
            return {}
        
        # Extract time components
        df['year'] = df['date_short'].dt.year
        df['month'] = df['date_short'].dt.month
        df['day'] = df['date_short'].dt.day
        df['dayofweek'] = df['date_short'].dt.dayofweek
        df['week'] = df['date_short'].dt.isocalendar().week
        df['quarter'] = df['date_short'].dt.quarter
        
        seasonality = {}
        
        # Monthly seasonality
        monthly_avg = df.groupby('month')[target_col].mean()
        seasonality['monthly_pattern'] = monthly_avg.to_dict()
        seasonality['monthly_std'] = df.groupby('month')[target_col].std().to_dict()
        
        # Weekly seasonality
        weekly_avg = df.groupby('dayofweek')[target_col].mean()
        seasonality['weekly_pattern'] = weekly_avg.to_dict()
        seasonality['weekly_std'] = df.groupby('dayofweek')[target_col].std().to_dict()
        
        # Quarterly seasonality
        quarterly_avg = df.groupby('quarter')[target_col].mean()
        seasonality['quarterly_pattern'] = quarterly_avg.to_dict()
        
        # Overall statistics
        seasonality['overall_mean'] = df[target_col].mean()
        seasonality['overall_std'] = df[target_col].std()
        seasonality['overall_min'] = df[target_col].min()
        seasonality['overall_max'] = df[target_col].max()
        
        return seasonality
    
    def detect_peaks(self, df, target_col='qtton', threshold=2.0):
        """Detect peaks in the data"""
        if df.empty or target_col not in df.columns:
            return {}
        
        values = df[target_col].dropna()
        if len(values) < 10:
            return {}
        
        # Calculate z-scores
        mean_val = values.mean()
        std_val = values.std()
        z_scores = (values - mean_val) / (std_val + 1e-8)
        
        # Identify peaks
        peaks = z_scores > threshold
        troughs = z_scores < -threshold
        
        peak_info = {
            'peak_count': peaks.sum(),
            'trough_count': troughs.sum(),
            'peak_percentage': (peaks.sum() / len(values)) * 100,
            'peak_threshold': threshold,
            'mean_value': mean_val,
            'std_value': std_val,
            'max_value': values.max(),
            'min_value': values.min(),
            'peak_intensity': values.max() / (mean_val + 1e-8)
        }
        
        return peak_info
    
    def monte_carlo_forecast(self, sku_data, target_col='qtton', periods=14, 
                           start_date=None, n_simulations=1000, error_reduction=True):
        """Generate Monte Carlo forecast with seasonality and peaks"""
        print(f"\n🎲 MONTE CARLO FORECAST FOR {target_col.upper()}")
        print("=" * 50)
        
        if sku_data.empty:
            return None
        
        # Ensure date column
        if 'date_short' not in sku_data.columns:
            return None
        
        sku_data['date_short'] = pd.to_datetime(sku_data['date_short'], errors='coerce')
        sku_data = sku_data.dropna(subset=['date_short', target_col])
        
        if len(sku_data) < 10:
            print("⚠️  Insufficient data for forecasting")
            return None
        
        # Set start date
        if start_date is None:
            start_date = datetime.now().date()
        elif isinstance(start_date, str):
            start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        
        # Analyze seasonality and peaks
        seasonality = self.analyze_seasonality(sku_data, target_col)
        peak_info = self.detect_peaks(sku_data, target_col)
        
        print(f"📊 Data points: {len(sku_data)}")
        print(f"🔍 Seasonality detected: {len(seasonality.get('monthly_pattern', {}))} months")
        print(f"🔺 Peaks detected: {peak_info.get('peak_count', 0)} ({peak_info.get('peak_percentage', 0):.1f}%)")
        
        # Prepare simulation parameters first
        base_mean = seasonality.get('overall_mean', sku_data[target_col].mean())
        base_std = seasonality.get('overall_std', sku_data[target_col].std())
        
        # ERROR REDUCTION TECHNIQUES
        if error_reduction:
            print("🎯 Applying error reduction techniques...")
            
            # 1. Trend Analysis - Use recent data more heavily
            recent_data = sku_data.tail(min(14, len(sku_data)))  # Last 14 days or all if less
            recent_mean = recent_data[target_col].mean()
            recent_std = recent_data[target_col].std()
            
            # 2. Moving Average for trend
            if len(sku_data) >= 7:
                # Calculate moving average
                ma_values = []
                window = min(7, len(sku_data))
                for i in range(window, len(sku_data)):
                    ma_values.append(sku_data[target_col].iloc[i-window:i].mean())
                
                # Use MA trend for better forecasting
                trend_adjustment = ma_values[-1] / (base_mean + 1e-8) if ma_values else 1.0
                print(f"📈 Trend adjustment factor: {trend_adjustment:.3f}")
            else:
                trend_adjustment = 1.0
            
            # 3. Volatility adjustment - reduce noise for more stable forecasts
            volatility_factor = min(1.0, max(0.3, 1.0 - (recent_std / (recent_mean + 1e-8))))
            print(f"📊 Volatility factor: {volatility_factor:.3f}")
            
            # 4. Outlier detection and adjustment
            values = sku_data[target_col].dropna()
            q1 = values.quantile(0.25)
            q3 = values.quantile(0.75)
            iqr = q3 - q1
            outlier_threshold = 1.5 * iqr
            outliers = ((values < q1 - outlier_threshold) | (values > q3 + outlier_threshold)).sum()
            outlier_adjustment = 1.0 - (outliers / len(values)) * 0.1  # Reduce impact of outliers
            print(f"🔍 Outlier adjustment: {outlier_adjustment:.3f}")
            
            # Apply error reduction adjustments
            # Weight recent data more heavily
            base_mean = 0.7 * recent_mean + 0.3 * base_mean
            base_std = base_std * volatility_factor * outlier_adjustment
            print(f"🎯 Adjusted base mean: {base_mean:.2f} (was {seasonality.get('overall_mean', 0):.2f})")
            print(f"🎯 Adjusted base std: {base_std:.2f} (was {seasonality.get('overall_std', 0):.2f})")
        else:
            # Initialize default values for non-error-reduction mode
            trend_adjustment = 1.0
        
        # Generate forecasts for each day
        forecasts = []
        forecast_dates = []
        
        for day in range(periods):
            current_date = start_date + timedelta(days=day)
            current_month = current_date.month
            current_weekday = current_date.weekday()
            
            # Base forecast from historical mean
            base_forecast = base_mean
            
            # Apply seasonal adjustment
            monthly_pattern = seasonality.get('monthly_pattern', {})
            if current_month in monthly_pattern:
                monthly_factor = monthly_pattern[current_month] / (base_mean + 1e-8)
                base_forecast *= monthly_factor
            
            # Apply weekly pattern
            weekly_pattern = seasonality.get('weekly_pattern', {})
            if current_weekday in weekly_pattern:
                weekly_factor = weekly_pattern[current_weekday] / (base_mean + 1e-8)
                base_forecast *= weekly_factor
            
            # Generate Monte Carlo simulations
            daily_forecasts = []
            
            for _ in range(n_simulations):
                # Base value with seasonal adjustment
                forecast_value = base_forecast
                
                # IMPROVED ERROR REDUCTION IN SIMULATION
                if error_reduction:
                    # 1. Reduced random variation for more stable forecasts
                    random_factor = np.random.normal(1.0, 0.1)  # Reduced from 20% to 10%
                    forecast_value *= random_factor
                    
                    # 2. Apply trend adjustment
                    forecast_value *= trend_adjustment
                    
                    # 3. Smoother peak handling
                    peak_probability = peak_info.get('peak_percentage', 0) / 100
                    if np.random.random() < peak_probability * 0.5:  # Reduce peak probability
                        peak_intensity = peak_info.get('peak_intensity', 2.0)
                        peak_factor = np.random.uniform(1.2, min(peak_intensity, 1.8))  # Reduced peak intensity
                        forecast_value *= peak_factor
                    
                    # 4. Add mean reversion to prevent extreme values
                    if forecast_value > base_mean * 2.5:  # Cap extreme high values
                        forecast_value = base_mean * 2.0
                    elif forecast_value < base_mean * 0.3:  # Cap extreme low values
                        forecast_value = base_mean * 0.5
                else:
                    # Original simulation logic
                    random_factor = np.random.normal(1.0, 0.2)  # 20% standard deviation
                    forecast_value *= random_factor
                    
                    # Apply peak probability
                    peak_probability = peak_info.get('peak_percentage', 0) / 100
                    if np.random.random() < peak_probability:
                        peak_intensity = peak_info.get('peak_intensity', 2.0)
                        peak_factor = np.random.uniform(1.5, peak_intensity)
                        forecast_value *= peak_factor
                
                # Ensure non-negative
                forecast_value = max(0, forecast_value)
                daily_forecasts.append(forecast_value)
            
            # Calculate statistics for this day
            daily_forecasts = np.array(daily_forecasts)
            
            # POST-PROCESSING ERROR REDUCTION
            if error_reduction:
                # 1. Remove extreme outliers (top and bottom 1%)
                q01 = np.percentile(daily_forecasts, 1)
                q99 = np.percentile(daily_forecasts, 99)
                daily_forecasts = daily_forecasts[(daily_forecasts >= q01) & (daily_forecasts <= q99)]
                
                # 2. Use weighted mean (more weight to median-like values)
                if len(daily_forecasts) > 0:
                    # Calculate weighted mean giving more weight to values closer to median
                    median_val = np.median(daily_forecasts)
                    weights = np.exp(-0.5 * ((daily_forecasts - median_val) / (np.std(daily_forecasts) + 1e-8)) ** 2)
                    weights = weights / np.sum(weights)
                    weighted_mean = np.sum(daily_forecasts * weights)
                else:
                    weighted_mean = np.mean(daily_forecasts)
                
                # 3. Smooth the forecast using a combination of mean and median
                final_mean = 0.6 * weighted_mean + 0.4 * np.median(daily_forecasts)
            else:
                final_mean = np.mean(daily_forecasts)
            
            forecast_stats = {
                'date': current_date,
                'mean': final_mean,
                'median': np.median(daily_forecasts),
                'std': np.std(daily_forecasts),
                'min': np.min(daily_forecasts),
                'max': np.max(daily_forecasts),
                'p25': np.percentile(daily_forecasts, 25),
                'p75': np.percentile(daily_forecasts, 75),
                'p95': np.percentile(daily_forecasts, 95)
            }
            
            forecasts.append(forecast_stats)
            forecast_dates.append(current_date)
        
        # Calculate overall metrics
        all_simulations = []
        for forecast in forecasts:
            all_simulations.extend([
                forecast['mean'], forecast['p25'], forecast['p75'], 
                forecast['min'], forecast['max']
            ])
        
        results = {
            'forecasts': forecasts,
            'forecast_dates': forecast_dates,
            'seasonality': seasonality,
            'peak_info': peak_info,
            'simulation_stats': {
                'total_simulations': n_simulations * periods,
                'mean_forecast': np.mean([f['mean'] for f in forecasts]),
                'std_forecast': np.std([f['mean'] for f in forecasts]),
                'min_forecast': np.min([f['min'] for f in forecasts]),
                'max_forecast': np.max([f['max'] for f in forecasts])
            },
            'metadata': {
                'sku': sku_data['sku'].iloc[0] if 'sku' in sku_data.columns else 'Unknown',
                'target_col': target_col,
                'periods': periods,
                'start_date': start_date.isoformat(),
                'n_simulations': n_simulations,
                'data_points': len(sku_data),
                'generated_at': datetime.now().isoformat()
            }
        }
        
        return results
    
    def arima_forecast(self, data, target_col='qtton', periods=14, start_date=None):
        """ARIMA forecasting model"""
        try:
            if data.empty or target_col not in data.columns:
                return None
            
            # Prepare data
            if 'date_short' not in data.columns:
                return None
            
            data['date_short'] = pd.to_datetime(data['date_short'], errors='coerce')
            data = data.dropna(subset=['date_short', target_col])
            data = data.sort_values('date_short')
            
            if len(data) < 10:
                return None
            
            # Fit ARIMA model
            try:
                model = ARIMA(data[target_col], order=(1, 1, 1))
                fitted_model = model.fit()
                
                # Generate forecast
                forecast = fitted_model.forecast(steps=periods)
                forecast_ci = fitted_model.get_forecast(steps=periods).conf_int()
                
                # Create forecast dates
                if start_date is None:
                    start_date = datetime.now().date()
                elif isinstance(start_date, str):
                    start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
                
                forecasts = []
                for i in range(periods):
                    current_date = start_date + timedelta(days=i)
                    forecasts.append({
                        'date': current_date,
                        'mean': forecast.iloc[i],
                        'median': forecast.iloc[i],
                        'std': (forecast_ci.iloc[i, 1] - forecast_ci.iloc[i, 0]) / 4,
                        'min': forecast_ci.iloc[i, 0],
                        'max': forecast_ci.iloc[i, 1],
                        'p25': forecast.iloc[i] - (forecast_ci.iloc[i, 1] - forecast.iloc[i]) * 0.5,
                        'p75': forecast.iloc[i] + (forecast.iloc[i] - forecast_ci.iloc[i, 0]) * 0.5,
                        'p95': forecast_ci.iloc[i, 1]
                    })
                
                return {
                    'forecasts': forecasts,
                    'model_name': 'ARIMA',
                    'model_params': {'order': (1, 1, 1)},
                    'aic': fitted_model.aic,
                    'bic': fitted_model.bic
                }
                
            except Exception as e:
                print(f"ARIMA model failed: {e}")
                return None
                
        except Exception as e:
            print(f"ARIMA forecast error: {e}")
            return None
    
    def linear_regression_forecast(self, data, target_col='qtton', periods=14, start_date=None):
        """Linear Regression forecasting model"""
        try:
            if data.empty or target_col not in data.columns:
                return None
            
            # Prepare data
            if 'date_short' not in data.columns:
                return None
            
            data['date_short'] = pd.to_datetime(data['date_short'], errors='coerce')
            data = data.dropna(subset=['date_short', target_col])
            data = data.sort_values('date_short')
            
            if len(data) < 10:
                return None
            
            # Create features
            data['day_of_year'] = data['date_short'].dt.dayofyear
            data['day_of_week'] = data['date_short'].dt.dayofweek
            data['month'] = data['date_short'].dt.month
            data['trend'] = range(len(data))
            
            # Prepare training data
            X = data[['day_of_year', 'day_of_week', 'month', 'trend']].values
            y = data[target_col].values
            
            # Scale features
            scaler = StandardScaler()
            X_scaled = scaler.fit_transform(X)
            
            # Fit model
            model = LinearRegression()
            model.fit(X_scaled, y)
            
            # Generate forecast
            if start_date is None:
                start_date = datetime.now().date()
            elif isinstance(start_date, str):
                start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
            
            forecasts = []
            last_trend = data['trend'].iloc[-1]
            
            for i in range(periods):
                current_date = start_date + timedelta(days=i)
                day_of_year = current_date.timetuple().tm_yday
                day_of_week = current_date.weekday()
                month = current_date.month
                trend = last_trend + i + 1
                
                X_pred = np.array([[day_of_year, day_of_week, month, trend]])
                X_pred_scaled = scaler.transform(X_pred)
                
                prediction = model.predict(X_pred_scaled)[0]
                
                forecasts.append({
                    'date': current_date,
                    'mean': prediction,
                    'median': prediction,
                    'std': data[target_col].std() * 0.15,
                    'min': prediction * 0.7,
                    'max': prediction * 1.3,
                    'p25': prediction * 0.85,
                    'p75': prediction * 1.15,
                    'p95': prediction * 1.25
                })
            
            return {
                'forecasts': forecasts,
                'model_name': 'Linear Regression',
                'model_params': {'features': ['day_of_year', 'day_of_week', 'month', 'trend']},
                'r2_score': model.score(X_scaled, y)
            }
            
        except Exception as e:
            print(f"Linear Regression forecast error: {e}")
            return None
    
    def ridge_regression_forecast(self, data, target_col='qtton', periods=14, start_date=None):
        """Ridge Regression forecasting model"""
        try:
            # Use helper method to prepare features
            prepared_data, feature_cols, last_values = self.prepare_features_for_ml_models(data, target_col)
            if prepared_data is None:
                return None
            
            # Prepare training data
            X = prepared_data[feature_cols].values
            y = prepared_data[target_col].values
            
            # Scale features
            scaler = StandardScaler()
            X_scaled = scaler.fit_transform(X)
            
            # Fit Ridge model
            model = Ridge(alpha=1.0, random_state=42)
            model.fit(X_scaled, y)
            
            # Generate forecast
            if start_date is None:
                start_date = datetime.now().date()
            elif isinstance(start_date, str):
                start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
            
            forecasts = []
            last_trend = prepared_data['trend'].iloc[-1]
            
            for i in range(periods):
                current_date = start_date + timedelta(days=i)
                day_of_year = current_date.timetuple().tm_yday
                day_of_week = current_date.weekday()
                month = current_date.month
                trend = last_trend + i + 1
                
                # Create features based on available feature set
                if 'lag_1' in feature_cols:
                    # Create lag features
                    lag_features = []
                    for lag in [1, 2, 3, 7]:
                        if i >= lag:
                            lag_features.append(forecasts[i-lag]['mean'])
                        else:
                            lag_features.append(last_values[-lag] if len(last_values) >= lag else last_values[0])
                    
                    X_pred = np.array([[day_of_year, day_of_week, month, trend] + lag_features])
                else:
                    # Use basic features only
                    X_pred = np.array([[day_of_year, day_of_week, month, trend]])
                
                X_pred_scaled = scaler.transform(X_pred)
                prediction = model.predict(X_pred_scaled)[0]
                
                forecasts.append({
                    'date': current_date,
                    'mean': prediction,
                    'median': prediction,
                    'std': prepared_data[target_col].std() * 0.12,
                    'min': prediction * 0.75,
                    'max': prediction * 1.25,
                    'p25': prediction * 0.9,
                    'p75': prediction * 1.1,
                    'p95': prediction * 1.2
                })
            
            return {
                'forecasts': forecasts,
                'model_name': 'Ridge Regression',
                'model_params': {'alpha': 1.0, 'features': feature_cols},
                'r2_score': model.score(X_scaled, y),
                'coefficients': dict(zip(feature_cols, model.coef_))
            }
            
        except Exception as e:
            print(f"Ridge Regression forecast error: {e}")
            return None
    
    def lasso_regression_forecast(self, data, target_col='qtton', periods=14, start_date=None):
        """Lasso Regression forecasting model"""
        try:
            if data.empty or target_col not in data.columns:
                return None
            
            # Prepare data
            if 'date_short' not in data.columns:
                return None
            
            data['date_short'] = pd.to_datetime(data['date_short'], errors='coerce')
            data = data.dropna(subset=['date_short', target_col])
            data = data.sort_values('date_short')
            
            if len(data) < 10:
                return None
            
            # Create features
            data['day_of_year'] = data['date_short'].dt.dayofyear
            data['day_of_week'] = data['date_short'].dt.dayofweek
            data['month'] = data['date_short'].dt.month
            data['trend'] = range(len(data))
            
            # Add lag features
            for lag in [1, 2, 3, 7]:
                data[f'lag_{lag}'] = data[target_col].shift(lag)
            
            data = data.dropna()
            
            if len(data) < 10:
                return None
            
            # Prepare training data
            feature_cols = ['day_of_year', 'day_of_week', 'month', 'trend', 'lag_1', 'lag_2', 'lag_3', 'lag_7']
            X = data[feature_cols].values
            y = data[target_col].values
            
            # Scale features
            scaler = StandardScaler()
            X_scaled = scaler.fit_transform(X)
            
            # Fit Lasso model
            model = Lasso(alpha=0.1, random_state=42)
            model.fit(X_scaled, y)
            
            # Generate forecast
            if start_date is None:
                start_date = datetime.now().date()
            elif isinstance(start_date, str):
                start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
            
            forecasts = []
            last_values = data[target_col].tail(7).tolist()
            last_trend = data['trend'].iloc[-1]
            
            for i in range(periods):
                current_date = start_date + timedelta(days=i)
                day_of_year = current_date.timetuple().tm_yday
                day_of_week = current_date.weekday()
                month = current_date.month
                trend = last_trend + i + 1
                
                # Create lag features
                lag_features = []
                for lag in [1, 2, 3, 7]:
                    if i >= lag:
                        lag_features.append(forecasts[i-lag]['mean'])
                    else:
                        lag_features.append(last_values[-lag] if len(last_values) >= lag else last_values[0])
                
                X_pred = np.array([[day_of_year, day_of_week, month, trend] + lag_features])
                X_pred_scaled = scaler.transform(X_pred)
                prediction = model.predict(X_pred_scaled)[0]
                
                forecasts.append({
                    'date': current_date,
                    'mean': prediction,
                    'median': prediction,
                    'std': data[target_col].std() * 0.13,
                    'min': prediction * 0.7,
                    'max': prediction * 1.3,
                    'p25': prediction * 0.85,
                    'p75': prediction * 1.15,
                    'p95': prediction * 1.25
                })
            
            return {
                'forecasts': forecasts,
                'model_name': 'Lasso Regression',
                'model_params': {'alpha': 0.1, 'features': feature_cols},
                'r2_score': model.score(X_scaled, y),
                'coefficients': dict(zip(feature_cols, model.coef_)),
                'selected_features': [col for col, coef in zip(feature_cols, model.coef_) if abs(coef) > 1e-6]
            }
            
        except Exception as e:
            print(f"Lasso Regression forecast error: {e}")
            return None
    
    def xgboost_forecast(self, data, target_col='qtton', periods=14, start_date=None):
        """XGBoost forecasting model"""
        try:
            if data.empty or target_col not in data.columns:
                return None
            
            # Prepare data
            if 'date_short' not in data.columns:
                return None
            
            data['date_short'] = pd.to_datetime(data['date_short'], errors='coerce')
            data = data.dropna(subset=['date_short', target_col])
            data = data.sort_values('date_short')
            
            if len(data) < 20:
                return None
            
            # Create features
            data['day_of_year'] = data['date_short'].dt.dayofyear
            data['day_of_week'] = data['date_short'].dt.dayofweek
            data['month'] = data['date_short'].dt.month
            data['trend'] = range(len(data))
            
            # Add lag features
            for lag in [1, 2, 3, 7, 14]:
                data[f'lag_{lag}'] = data[target_col].shift(lag)
            
            # Add rolling statistics
            for window in [3, 7]:
                data[f'rolling_mean_{window}'] = data[target_col].rolling(window=window).mean()
                data[f'rolling_std_{window}'] = data[target_col].rolling(window=window).std()
            
            data = data.dropna()
            
            if len(data) < 15:
                return None
            
            # Prepare training data
            feature_cols = ['day_of_year', 'day_of_week', 'month', 'trend', 
                          'lag_1', 'lag_2', 'lag_3', 'lag_7', 'lag_14',
                          'rolling_mean_3', 'rolling_mean_7', 'rolling_std_3', 'rolling_std_7']
            
            # Remove any columns that don't exist
            feature_cols = [col for col in feature_cols if col in data.columns]
            
            X = data[feature_cols].values
            y = data[target_col].values
            
            # Fit XGBoost model
            model = xgb.XGBRegressor(
                n_estimators=100,
                max_depth=6,
                learning_rate=0.1,
                random_state=42,
                n_jobs=-1
            )
            model.fit(X, y)
            
            # Generate forecast
            if start_date is None:
                start_date = datetime.now().date()
            elif isinstance(start_date, str):
                start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
            
            forecasts = []
            history = data[target_col].tail(14).tolist()
            last_trend = data['trend'].iloc[-1]

            for i in range(periods):
                current_date = start_date + timedelta(days=i)

                X_pred = np.array([[
                    current_date.timetuple().tm_yday,
                    current_date.weekday(),
                    current_date.month,
                    last_trend + i + 1,
                    *[history[-lag] if len(history) >= lag else history[0] for lag in [1,2,3,7,14]],
                    np.mean(history[-3:]),
                    np.mean(history[-7:]),
                    np.std(history[-3:]),
                    np.std(history[-7:])
                ]])

                prediction = model.predict(X_pred)[0]

                history.append(prediction)
                history = history[-14:]

                forecasts.append({
                    'date': current_date,
                    'mean': prediction,
                    'median': prediction,
                    'std': np.std(history[-7:]),
                    'min': prediction * 0.9,
                    'max': prediction * 1.1,
                    'p25': prediction * 0.95,
                    'p75': prediction * 1.05,
                    'p95': prediction * 1.1
                })
            
            return {
                'forecasts': forecasts,
                'model_name': 'XGBoost',
                'model_params': {'n_estimators': 100, 'max_depth': 6, 'learning_rate': 0.1, 'features': feature_cols},
                'feature_importance': dict(zip(feature_cols, model.feature_importances_)),
                'r2_score': model.score(X, y)
            }
            
        except Exception as e:
            print(f"XGBoost forecast error: {e}")
            return None
    
    def random_forest_forecast(self, data, target_col='qtton', periods=14, start_date=None):
        """Random Forest forecasting model"""
        try:
            if data.empty or target_col not in data.columns:
                return None
            
            # Prepare data
            if 'date_short' not in data.columns:
                return None
            
            data['date_short'] = pd.to_datetime(data['date_short'], errors='coerce')
            data = data.dropna(subset=['date_short', target_col])
            data = data.sort_values('date_short')
            
            if len(data) < 20:
                return None
            
            # Create features
            data['day_of_year'] = data['date_short'].dt.dayofyear
            data['day_of_week'] = data['date_short'].dt.dayofweek
            data['month'] = data['date_short'].dt.month
            data['trend'] = range(len(data))
            
            # Add lag features
            for lag in [1, 2, 3, 7]:
                data[f'lag_{lag}'] = data[target_col].shift(lag)
            
            data = data.dropna()
            
            if len(data) < 10:
                return None
            
            # Prepare training data
            feature_cols = ['day_of_year', 'day_of_week', 'month', 'trend', 'lag_1', 'lag_2', 'lag_3', 'lag_7']
            X = data[feature_cols].values
            y = data[target_col].values
            
            # Fit model
            model = RandomForestRegressor(n_estimators=100, random_state=42)
            model.fit(X, y)
            
            # Generate forecast
            if start_date is None:
                start_date = datetime.now().date()
            elif isinstance(start_date, str):
                start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
            
            forecasts = []
            last_values = data[target_col].tail(7).tolist()
            last_trend = data['trend'].iloc[-1]
            
            for i in range(periods):
                current_date = start_date + timedelta(days=i)
                day_of_year = current_date.timetuple().tm_yday
                day_of_week = current_date.weekday()
                month = current_date.month
                trend = last_trend + i + 1
                
                # Create lag features
                lag_features = []
                for lag in [1, 2, 3, 7]:
                    if i >= lag:
                        lag_features.append(forecasts[i-lag]['mean'])
                    else:
                        lag_features.append(last_values[-lag] if len(last_values) >= lag else last_values[0])
                
                X_pred = np.array([[day_of_year, day_of_week, month, trend] + lag_features])
                prediction = model.predict(X_pred)[0]
                
                # Get prediction intervals from ensemble
                predictions = []
                for tree in model.estimators_[:10]:  # Sample 10 trees for intervals
                    pred = tree.predict(X_pred)[0]
                    predictions.append(pred)
                
                predictions = np.array(predictions)
                
                forecasts.append({
                    'date': current_date,
                    'mean': prediction,
                    'median': np.median(predictions),
                    'std': np.std(predictions),
                    'min': np.min(predictions),
                    'max': np.max(predictions),
                    'p25': np.percentile(predictions, 25),
                    'p75': np.percentile(predictions, 75),
                    'p95': np.percentile(predictions, 95)
                })
            
            return {
                'forecasts': forecasts,
                'model_name': 'Random Forest',
                'model_params': {'n_estimators': 100, 'features': feature_cols},
                'feature_importance': dict(zip(feature_cols, model.feature_importances_))
            }
            
        except Exception as e:
            print(f"Random Forest forecast error: {e}")
            return None

#---------------NEW FEATURE: 3m/ 6m / 12m Training Windows------------------------#
    def generate_multi_window_forecasts(self, sku, metric='qtton', horizon_days=14, selected_models=None):
    # """
    # Returns dict: {window_id: {model_name: (dates, predictions)}}
    # """
        if selected_models is None or 'all' in selected_models:
            models_to_run = ['monte_carlo', 'arima', 'linear', 'ridge', 'lasso', 'rf', 'xgboost']
        else:
            models_to_run = selected_models

        all_results = {}
    
        for window in TRAINING_WINDOWS:
            wid = window['id']
            data = self.get_window_data(sku, metric, window['months'])
            if data is None:
                continue
            
            window_forecasts = {}
            for model_name in models_to_run:
            # Call your actual forecasting method
            # You need to adapt these calls to your real function names/signatures
                try:
                    if model_name == 'monte_carlo':
                        preds, _ = self.run_monte_carlo_forecast(data, horizon_days, metric)
                    elif model_name == 'arima':
                        preds, _ = self.run_arima_forecast(data, horizon_days, metric)
                    elif model_name == 'linear':
                        preds, _ = self.run_linear_regression(data, horizon_days, metric)
                    elif model_name == 'ridge':
                        preds, _ = self.run_ridge_regression(data, horizon_days, metric)
                    elif model_name == 'lasso':
                        preds, _ = self.run_lasso_regression(data, horizon_days, metric)
                    elif model_name == 'rf':
                        preds, _ = self.run_random_forest(data, horizon_days, metric)
                    elif model_name == 'xgboost':
                        preds, _ = self.run_xgboost_forecast(data, horizon_days, metric)
                    else:
                        continue
                    
                    if preds is not None:
                        dates = pd.date_range(
                            data['date_short'].max() + pd.Timedelta(days=1),
                            periods=horizon_days,
                            freq='D'
                        )
                        window_forecasts[model_name] = (dates, preds)
                except Exception as e:
                    print(f"Error in {model_name} for {wid}: {e}")
                
        if window_forecasts:
            all_results[wid] = window_forecasts
            
        return all_results 

#--------------------------------------------------------------------------------------#

    def compare_all_models(self, data, target_col='qtton', periods=14, start_date=None):
        """Compare all forecasting models"""
        print(f"\n🔍 COMPARING ALL FORECASTING MODELS")
        print("=" * 60)
        
        models = {}
        
        # Monte Carlo
        print("🎲 Running Monte Carlo simulation...")
        mc_result = self.monte_carlo_forecast(data, target_col, periods, start_date, n_simulations=500)
        if mc_result:
            models['Monte Carlo'] = mc_result
        
        # ARIMA
        print("📈 Running ARIMA model...")
        arima_result = self.arima_forecast(data, target_col, periods, start_date)
        if arima_result:
            models['ARIMA'] = arima_result
        
        # Linear Regression
        print("📉 Running Linear Regression...")
        lr_result = self.linear_regression_forecast(data, target_col, periods, start_date)
        if lr_result:
            models['Linear Regression'] = lr_result
        
        # Ridge Regression
        print("🏔️ Running Ridge Regression...")
        try:
            ridge_result = self.ridge_regression_forecast(data, target_col, periods, start_date)
            if ridge_result:
                models['Ridge Regression'] = ridge_result
                print("  ✅ Ridge Regression completed")
            else:
                print("  ❌ Ridge Regression failed - insufficient data or error")
        except Exception as e:
            print(f"  ❌ Ridge Regression error: {e}")
        
        # Lasso Regression
        print("🎯 Running Lasso Regression...")
        try:
            lasso_result = self.lasso_regression_forecast(data, target_col, periods, start_date)
            if lasso_result:
                models['Lasso Regression'] = lasso_result
                print("  ✅ Lasso Regression completed")
            else:
                print("  ❌ Lasso Regression failed - insufficient data or error")
        except Exception as e:
            print(f"  ❌ Lasso Regression error: {e}")
        
        # Random Forest
        print("🌲 Running Random Forest...")
        try:
            rf_result = self.random_forest_forecast(data, target_col, periods, start_date)
            if rf_result:
                models['Random Forest'] = rf_result
                print("  ✅ Random Forest completed")
            else:
                print("  ❌ Random Forest failed - insufficient data or error")
        except Exception as e:
            print(f"  ❌ Random Forest error: {e}")
        
        # XGBoost
        print("⚡ Running XGBoost...")
        xgb_result = self.xgboost_forecast(data, target_col, periods, start_date)
        if xgb_result:
            models['XGBoost'] = xgb_result
        
        print(f"✅ Successfully ran {len(models)} models")
        
        return models
    
    def run_single_model(self, model_name, data, target_col='qtton', periods=14, start_date=None, error_reduction=False):
        """Run a single forecasting model"""
        try:
            if model_name == 'monte_carlo':
                return self.monte_carlo_forecast(data, target_col, periods, start_date, n_simulations=1000, error_reduction=error_reduction)
            elif model_name == 'arima':
                return self.arima_forecast(data, target_col, periods, start_date)
            elif model_name == 'linear_regression':
                return self.linear_regression_forecast(data, target_col, periods, start_date)
            elif model_name == 'ridge_regression':
                return self.ridge_regression_forecast(data, target_col, periods, start_date)
            elif model_name == 'lasso_regression':
                return self.lasso_regression_forecast(data, target_col, periods, start_date)
            elif model_name == 'random_forest':
                return self.random_forest_forecast(data, target_col, periods, start_date)
            elif model_name == 'xgboost':
                return self.xgboost_forecast(data, target_col, periods, start_date)
            else:
                return None
        except Exception as e:
            print(f"Error running {model_name}: {e}")
            return None

#---------------NEW Feature: Windowded Model Comparison Bases Training Windows------------------------#
    def create_multi_window_comparison_chart(self, all_results, window_labels, active_tab, metric, sku):
        fig = go.Figure()

        for w_value, window_data in all_results.items():
            label = window_labels[w_value]
            is_active = (w_value == active_tab)

            for model_name, result in window_data.items():
                dates = pd.date_range(start=result.get('start_date', datetime.now()), periods=len(result['prediction']), freq='D')
                preds = result['prediction']

                fig.add_trace(go.Scatter(
                    x=dates,
                    y=preds,
                    mode='lines',
                    name=f"{model_name.title()} - {label}",
                    line=dict(
                        width=3.5 if model_name == 'monte_carlo' else 2.5,
                        dash='dash' if not is_active else 'solid'
                    ),
                    visible=is_active or 'legendonly'
                ))

        fig.update_layout(
            title=f"{sku} Forecast Comparison (Different Training Periods)",
            xaxis_title="Forecast Date",
            yaxis_title=metric.upper(),
            hovermode="x unified",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
            height=600,
            margin=dict(l=50, r=30, t=80, b=60)
        )

        return fig
    
#--------------------------------------------------------------------------------------#

    def create_model_comparison_chart(self, models_result, target_col='qtton'):
        """Create a chart comparing all models"""
        try:
            fig = go.Figure()
            
            colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd', '#8c564b', '#e377c2']
            
            for i, (model_name, model_result) in enumerate(models_result.items()):
                if model_result and 'forecasts' in model_result:
                    forecasts = model_result['forecasts']
                    dates = [f['date'] for f in forecasts]
                    means = [f['mean'] for f in forecasts]
                    
                    fig.add_trace(go.Scatter(
                        x=dates,
                        y=means,
                        mode='lines+markers',
                        name=model_name,
                        line=dict(color=colors[i % len(colors)], width=2),
                        marker=dict(size=6)
                    ))
            
            fig.update_layout(
                title=f'Model Comparison - {target_col.upper()}',
                xaxis_title='Date',
                yaxis_title=f'{target_col.upper()}',
                hovermode='x unified',
                template='plotly_white',
                height=500
            )
            
            return fig
            
        except Exception as e:
            print(f"Error creating model comparison chart: {e}")
            return go.Figure()
    
    def create_model_comparison_analysis(self, models_result):
        """Create analysis chart for model comparison"""
        try:
            fig = go.Figure()
            
            model_names = []
            r2_scores = []
            
            for model_name, model_result in models_result.items():
                if model_result:
                    model_names.append(model_name)
                    if 'r2_score' in model_result:
                        r2_scores.append(model_result['r2_score'])
                    else:
                        r2_scores.append(0)
            
            fig.add_trace(go.Bar(
                x=model_names,
                y=r2_scores,
                marker_color='lightblue',
                text=[f'{score:.3f}' for score in r2_scores],
                textposition='auto'
            ))
            
            fig.update_layout(
                title='Model Performance Comparison (R² Score)',
                xaxis_title='Model',
                yaxis_title='R² Score',
                template='plotly_white',
                height=400
            )
            
            return fig
            
        except Exception as e:
            print(f"Error creating model comparison analysis: {e}")
            return go.Figure()
    
    def create_model_comparison_metrics(self, models_result, actual_data, target_col='qtton'):
        """Create metrics table for model comparison"""
        try:
            metrics_data = []
            
            for model_name, model_result in models_result.items():
                if model_result and 'forecasts' in model_result:
                    forecasts = model_result['forecasts']
                    predicted_values = [f['mean'] for f in forecasts]
                    
                    # Get actual values for comparison (last few days)
                    actual_values = actual_data[target_col].tail(len(predicted_values)).tolist()
                    
                    if len(actual_values) > 0 and len(predicted_values) > 0:
                        metrics = self.calculate_metrics(actual_values, predicted_values)
                        
                        metrics_data.append({
                            'Model': model_name,
                            'MAE': f"{metrics['mae']:.2f}",
                            'MAPE': f"{metrics['mape']:.2f}%",
                            'RMSE': f"{metrics['rmse']:.2f}",
                            'R²': f"{model_result.get('r2_score', 0):.3f}" if 'r2_score' in model_result else "N/A"
                        })
            
            if not metrics_data:
                return "No metrics available"
            
            return dash_table.DataTable(
                data=metrics_data,
                columns=[
                    {'name': 'Model', 'id': 'Model'},
                    {'name': 'MAE', 'id': 'MAE'},
                    {'name': 'MAPE', 'id': 'MAPE'},
                    {'name': 'RMSE', 'id': 'RMSE'},
                    {'name': 'R²', 'id': 'R²'}
                ],
                style_cell={'textAlign': 'center'},
                style_header={'backgroundColor': 'lightblue', 'fontWeight': 'bold'},
                style_data_conditional=[
                    {
                        'if': {'row_index': 0},
                        'backgroundColor': 'lightgreen',
                    }
                ]
            )
            
        except Exception as e:
            print(f"Error creating model comparison metrics: {e}")
            return "Error creating metrics table"
    
    def calculate_metrics(self, actual, predicted):
        """Calculate MAE, MAPE, and RMSE with improved MAPE handling"""
        actual = np.array(actual)
        predicted = np.array(predicted)
        
        if len(actual) == 0:
            return {'mae': np.nan, 'mape': np.nan, 'rmse': np.nan}
        
        # Calculate metrics
        mae = np.mean(np.abs(actual - predicted))
        
        # Fix MAPE calculation to handle zero and very small values
        y_test_safe = np.where(actual == 0, 1e-8, actual)
        mape = np.mean(np.abs((actual - predicted) / y_test_safe)) * 100
        
        # Cap MAPE at reasonable values to avoid extreme percentages
        mape = min(mape, 1000)  # Cap at 1000%
        
        rmse = np.sqrt(np.mean((actual - predicted) ** 2))
        
        return {
            'mae': mae,
            'mape': mape,
            'rmse': rmse,
            'n_points': len(actual)
        }
        
#--------------NEW FEATURE: For Best Model Metrics Windowed -----------------------#
    def compute_metrics_from_result(self, result, historical_data, target_col='qtton'):
        """
        Compute MAE, MAPE, RMSE from forecast result against recent actuals
        (adapt to your actual metric calculation logic)
        """
        if not result or 'forecasts' not in result:
            return {}

        preds = [day['mean'] for day in result['forecasts']]
        if not preds:
            return {}

        # Example: compare last few actuals to first few preds (or your real backtest logic)
        actuals = historical_data[target_col].tail(len(preds)).values if len(historical_data) >= len(preds) else []

        if len(actuals) != len(preds):
            return {'MAE': None, 'MAPE': None, 'RMSE': None}

        from sklearn.metrics import mean_absolute_error, mean_squared_error
        import numpy as np

        mae = mean_absolute_error(actuals, preds) if len(actuals) > 0 else None
        rmse = np.sqrt(mean_squared_error(actuals, preds)) if len(actuals) > 0 else None
        mape = np.mean(np.abs((actuals - preds) / (actuals + 1e-8))) * 100 if len(actuals) > 0 else None

        return {
            'MAE': mae,
            'MAPE': mape,
            'RMSE': rmse
        }
#----------------------------------------------------------------------#   
    
    def get_all_skus_metrics(self, target_col='qtton', periods=14, start_date=None):
        """Calculate overall metrics for all SKUs"""
        print(f"\n📊 CALCULATING OVERALL METRICS FOR ALL SKUs")
        print("=" * 60)
        
        skus = self.get_available_skus()
        if not skus:
            return None
        
        all_actuals = []
        all_predictions = []
        sku_results = []
        
        for sku in skus[:20]:  # Limit to first 20 SKUs for performance
            print(f"📈 Processing SKU: {sku}")
            
            try:
                sku_data = self.get_sku_data(sku)
                if sku_data.empty:
                    continue
                
                # Generate forecast
                forecast_result = self.monte_carlo_forecast(
                    sku_data, target_col, periods, start_date, n_simulations=100
                )
                
                if forecast_result is None:
                    continue
                
                # Get recent actual data for comparison
                recent_data = sku_data.head(periods)
                if len(recent_data) < periods:
                    continue
                
                actual_values = recent_data[target_col].tolist()[:periods]
                predicted_values = [f['mean'] for f in forecast_result['forecasts']]
                
                # Calculate metrics for this SKU
                metrics = self.calculate_metrics(actual_values, predicted_values)
                metrics['sku'] = sku
                
                sku_results.append(metrics)
                
                # Add to overall arrays
                all_actuals.extend(actual_values)
                all_predictions.extend(predicted_values)
                
                print(f"  ✅ MAE: {metrics['mae']:.2f}, MAPE: {metrics['mape']:.1f}%, RMSE: {metrics['rmse']:.2f}")
                
            except Exception as e:
                print(f"  ❌ Error processing {sku}: {e}")
                continue
        
        # Calculate overall metrics
        overall_metrics = self.calculate_metrics(all_actuals, all_predictions)
        overall_metrics['total_skus'] = len(sku_results)
        overall_metrics['total_data_points'] = len(all_actuals)
        
        results = {
            'overall_metrics': overall_metrics,
            'sku_results': sku_results,
            'metadata': {
                'target_col': target_col,
                'periods': periods,
                'start_date': start_date.isoformat() if start_date else None,
                'processed_skus': len(sku_results),
                'generated_at': datetime.now().isoformat()
            }
        }
        
        print(f"\n🎯 OVERALL METRICS:")
        print(f"  MAE: {overall_metrics['mae']:.2f}")
        print(f"  MAPE: {overall_metrics['mape']:.1f}%")
        print(f"  RMSE: {overall_metrics['rmse']:.2f}")
        print(f"  Total SKUs: {overall_metrics['total_skus']}")
        print(f"  Data Points: {overall_metrics['total_data_points']}")
        
        return results
    
    def generate_comprehensive_report(self, target_col='qtton', start_date=None, level='sku', include_historical=True, use_all_models=True):
        """Generate comprehensive predictions report for all SKUs or families across multiple time horizons with all models"""
        print(f"\n📊 GENERATING COMPREHENSIVE {level.upper()} PREDICTIONS REPORT")
        print("=" * 70)
        
        # Get items based on level
        if level.lower() == 'family':
            items = self.get_available_families()
            item_type = 'families'
        else:
            items = self.get_available_skus()
            item_type = 'SKUs'
        
        if not items:
            print(f"❌ No {item_type} found")
            return None
        
        # Define time horizons - both forward and historical
        time_horizons = {
            '1_day': 1,
            '1_week': 7,
            '2_weeks': 14
        }
        
        if include_historical:
            time_horizons.update({
                'last_10_days': -10,
                'last_10_weeks': -70,
                'last_10_fortnights': -140
            })
        
        if start_date is None:
            start_date = datetime.now().date()
        elif isinstance(start_date, str):
            start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        
        print(f"🎯 Processing {len(items)} {item_type} across {len(time_horizons)} time horizons")
        print(f"📅 Start date: {start_date}")
        print(f"📊 Target metric: {target_col}")
        print(f"📈 Level: {level.upper()}")
        print(f"🤖 Use all models: {use_all_models}")
        
        all_predictions = []
        processed_items = 0
        failed_items = 0
        
        for i, item in enumerate(items, 1):
            print(f"\n📈 Processing {level.upper()} {i}/{len(items)}: {item}")
            
            try:
                # Get data based on level
                if level.lower() == 'family':
                    item_data = self.get_family_data(item)
                    if not item_data.empty:
                        item_data = self.aggregate_family_data(item_data, target_col)
                else:
                    item_data = self.get_sku_data(item)
                
                if item_data.empty:
                    print(f"  ⚠️  No data found for {level}: {item}")
                    failed_items += 1
                    continue
                
                # Generate predictions for each time horizon
                item_predictions = {
                    'item': item,
                    'level': level,
                    'data_points': len(item_data),
                    'predictions': {}
                }
                
                for horizon_name, periods in time_horizons.items():
                    print(f"  🔮 Generating {horizon_name} {'forecast' if periods > 0 else 'analysis'}...")
                    
                    if periods > 0:
                        # Forward-looking forecast with all models
                        # Limit historical data based on forecast period for comprehensive report
                        limited_data = self._limit_historical_data_for_forecast(item_data, target_col, periods)
                        
                        if use_all_models:
                            models_result = self.compare_all_models(
                                limited_data, target_col, periods, start_date
                            )
                            
                            if not models_result:
                                print(f"    ❌ Failed to generate {horizon_name} forecasts")
                                item_predictions['predictions'][horizon_name] = None
                                continue
                            
                            # Calculate metrics for each model
                            model_metrics = {}
                            for model_name, model_result in models_result.items():
                                if model_result and 'forecasts' in model_result:
                                    metrics = self.calculate_metrics(limited_data['qtton'], model_result['forecasts'][0]['mean'])
                                    model_metrics[model_name] = metrics
                                    
                                        
                            
                            # Store all model results
                            prediction_data = {
                                'periods': periods,
                                'start_date': start_date.isoformat(),
                                'type': 'forecast',
                                'models': models_result,
                                'model_comparison': self._create_model_comparison(models_result),
                                'metrics': model_metrics
                            }
                            
                        else:
                            # Use only Monte Carlo
                            forecast_result = self.monte_carlo_forecast(
                                limited_data, target_col, periods, start_date, 
                                n_simulations=500, error_reduction=True
                            )
                            
                            if forecast_result is None:
                                print(f"    ❌ Failed to generate {horizon_name} forecast")
                                item_predictions['predictions'][horizon_name] = None
                                continue
                            
                            # Calculate metrics for Monte Carlo
                            monte_carlo_metrics = self.calculate_metrics(limited_data, forecast_result)
                            
                            prediction_data = {
                                'periods': periods,
                                'start_date': start_date.isoformat(),
                                'type': 'forecast',
                                'models': {'Monte Carlo': forecast_result},
                                'model_comparison': None,
                                'metrics': {'Monte Carlo': monte_carlo_metrics}
                            }
                        
                    else:
                        # Historical analysis
                        historical_data = self._get_historical_data(item_data, abs(periods), target_col)
                        
                        if historical_data.empty:
                            print(f"    ❌ Failed to get {horizon_name} historical data")
                            item_predictions['predictions'][horizon_name] = None
                            continue
                        
                        prediction_data = {
                            'periods': abs(periods),
                            'start_date': historical_data['date_short'].min().isoformat(),
                            'end_date': historical_data['date_short'].max().isoformat(),
                            'type': 'historical',
                            'models': {'Historical': {'forecasts': []}},
                            'model_comparison': None
                        }
                        
                        # Convert historical data to forecast format
                        for _, row in historical_data.iterrows():
                            prediction_data['models']['Historical']['forecasts'].append({
                                'date': row['date_short'].isoformat(),
                                'mean': row[target_col],
                                'median': row[target_col],
                                'std': 0,
                                'min': row[target_col],
                                'max': row[target_col],
                                'p25': row[target_col],
                                'p75': row[target_col],
                                'p95': row[target_col]
                            })
                    
                    # Calculate summary statistics for each model
                    model_summaries = {}
                    for model_name, model_result in prediction_data['models'].items():
                        if model_result and 'forecasts' in model_result:
                            forecasts = model_result['forecasts']
                            if forecasts:
                                means = [f['mean'] for f in forecasts]
                                model_summaries[model_name] = {
                                    'total_predicted': sum(means),
                                    'daily_average': np.mean(means),
                                    'daily_std': np.std(means),
                                    'min_daily': min(means),
                                    'max_daily': max(means),
                                    'trend': 'increasing' if means[-1] > means[0] else 'decreasing' if means[-1] < means[0] else 'stable'
                                }
                    
                    prediction_data['summary'] = model_summaries
                    item_predictions['predictions'][horizon_name] = prediction_data
                    
                    # Print summary for first model (Monte Carlo or first available)
                    first_model = list(model_summaries.keys())[0] if model_summaries else 'N/A'
                    if first_model != 'N/A':
                        summary = model_summaries[first_model]
                        print(f"    ✅ {horizon_name}: {summary['total_predicted']:.2f} total, {summary['daily_average']:.2f} avg/day ({len(model_summaries)} models)")
                
                all_predictions.append(item_predictions)
                processed_items += 1
                
            except Exception as e:
                print(f"  ❌ Error processing {level} {item}: {e}")
                failed_items += 1
                continue
        
        # Create comprehensive report
        report = {
            'metadata': {
                'generated_at': datetime.now().isoformat(),
                'start_date': start_date.isoformat(),
                'target_col': target_col,
                'level': level,
                'total_items': len(items),
                'processed_items': processed_items,
                'failed_items': failed_items,
                'time_horizons': time_horizons,
                'include_historical': include_historical,
                'use_all_models': use_all_models
            },
            'predictions': all_predictions,
            'summary': self._create_report_summary(all_predictions, time_horizons)
        }
        
        print(f"\n🎉 COMPREHENSIVE REPORT GENERATED!")
        print("=" * 70)
        print(f"📊 Total {item_type}: {len(items)}")
        print(f"✅ Processed: {processed_items}")
        print(f"❌ Failed: {failed_items}")
        print(f"📈 Time horizons: {', '.join(time_horizons.keys())}")
        print(f"🤖 Models used: {'All models' if use_all_models else 'Monte Carlo only'}")
        
        return report
    
    def _create_model_comparison(self, models_result):
        """Create model comparison summary"""
        comparison = {}
        
        for model_name, model_result in models_result.items():
            if model_result and 'forecasts' in model_result:
                forecasts = model_result['forecasts']
                if forecasts:
                    means = [f['mean'] for f in forecasts]
                    comparison[model_name] = {
                        'total_forecast': sum(means),
                        'avg_daily': np.mean(means),
                        'std_daily': np.std(means),
                        'trend': 'increasing' if means[-1] > means[0] else 'decreasing' if means[-1] < means[0] else 'stable',
                        'model_info': {
                            'aic': model_result.get('aic', 'N/A'),
                            'bic': model_result.get('bic', 'N/A'),
                            'r2_score': model_result.get('r2_score', 'N/A')
                        }
                    }
        
        return comparison
    
    def _get_historical_data(self, item_data, periods, target_col):
        """Get historical data for the specified number of periods"""
        if item_data.empty or 'date_short' not in item_data.columns:
            return pd.DataFrame()
        
        # Ensure date column is datetime
        item_data['date_short'] = pd.to_datetime(item_data['date_short'], errors='coerce')
        item_data = item_data.dropna(subset=['date_short', target_col])
        
        if item_data.empty:
            return pd.DataFrame()
        
        # Sort by date and get the last N periods
        item_data_sorted = item_data.sort_values('date_short', ascending=False)
        historical_data = item_data_sorted.head(periods)
        
        return historical_data.sort_values('date_short', ascending=True)
    
    def generate_comprehensive_sku_report(self, target_col='qtton', start_date=None):
        """Legacy method for backward compatibility"""
        return self.generate_comprehensive_report(target_col, start_date, level='sku', include_historical=False)
    
    def _create_report_summary(self, predictions, time_horizons):
        """Create summary statistics for the report"""
        summary = {}
        
        for horizon_name in time_horizons.keys():
            horizon_data = []
            all_metrics = {'mae': [], 'mape': [], 'rmse': []}
            
            for item_pred in predictions:
                horizon_pred = item_pred['predictions'].get(horizon_name)
                if horizon_pred and horizon_pred.get('summary'):
                    # Handle multiple models in summary
                    if isinstance(horizon_pred['summary'], dict):
                        # Get the first model's summary for overall statistics
                        first_model = list(horizon_pred['summary'].keys())[0]
                        model_summary = horizon_pred['summary'][first_model]
                        
                        horizon_data.append({
                            'item': item_pred['item'],
                            'total_predicted': model_summary['total_predicted'],
                            'daily_average': model_summary['daily_average'],
                            'trend': model_summary['trend']
                        })
                
                # Collect metrics if available
                if horizon_pred and horizon_pred.get('metrics'):
                    for model_name, metrics in horizon_pred['metrics'].items():
                        if metrics:
                            all_metrics['mae'].append(metrics.get('mae', 0))
                            all_metrics['mape'].append(metrics.get('mape', 0))
                            all_metrics['rmse'].append(metrics.get('rmse', 0))
            
            if horizon_data:
                totals = [d['total_predicted'] for d in horizon_data]
                daily_avgs = [d['daily_average'] for d in horizon_data]
                
                summary[horizon_name] = {
                    'total_skus': len(horizon_data),
                    'grand_total': sum(totals),
                    'average_per_sku': np.mean(totals),
                    'median_per_sku': np.median(totals),
                    'std_per_sku': np.std(totals),
                    'min_sku_total': min(totals),
                    'max_sku_total': max(totals),
                    'average_daily_per_sku': np.mean(daily_avgs),
                    'trend_distribution': {
                        'increasing': sum(1 for d in horizon_data if d['trend'] == 'increasing'),
                        'decreasing': sum(1 for d in horizon_data if d['trend'] == 'decreasing'),
                        'stable': sum(1 for d in horizon_data if d['trend'] == 'stable')
                    },
                    'average_metrics': {
                        'mae': np.mean(all_metrics['mae']) if all_metrics['mae'] else 0,
                        'mape': np.mean(all_metrics['mape']) if all_metrics['mape'] else 0,
                        'rmse': np.mean(all_metrics['rmse']) if all_metrics['rmse'] else 0
                    }
                }
        
        return summary
    
    def export_predictions_to_csv(self, report, filename=None):
        """Export predictions report to CSV files"""
        if filename is None:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            level = report['metadata']['level']
            filename = f'{level}_predictions_report_{timestamp}'
        
        print(f"\n📁 EXPORTING PREDICTIONS TO CSV")
        print("=" * 50)
        
        exported_files = []
        metadata = report['metadata']
        time_horizons = metadata['time_horizons']
        level_name = metadata['level'].upper()
        
        # Export detailed predictions for each time horizon
        for horizon_name in time_horizons.keys():
            horizon_data = []
            
            for item_pred in report['predictions']:
                item = item_pred['item']
                horizon_pred = item_pred['predictions'].get(horizon_name)
                
                if horizon_pred and horizon_pred.get('models'):
                    # Handle multiple models
                    for model_name, model_result in horizon_pred['models'].items():
                        if model_result and 'forecasts' in model_result:
                            for forecast in model_result['forecasts']:
                                # Get metrics for this model if available
                                model_metrics = horizon_pred.get('metrics', {}).get(model_name, {})
                                
                                horizon_data.append({
                                    level_name: item,
                                    'Model': model_name,
                                    'Date': forecast['date'],
                                    'Mean_Prediction': forecast['mean'],
                                    'Median_Prediction': forecast['median'],
                                    'Std_Deviation': forecast['std'],
                                    'Min_Prediction': forecast['min'],
                                    'Max_Prediction': forecast['max'],
                                    'P25': forecast['p25'],
                                    'P75': forecast['p75'],
                                    'P95': forecast['p95'],
                                    'MAE': model_metrics.get('mae', ''),
                                    'MAPE': model_metrics.get('mape', ''),
                                    'RMSE': model_metrics.get('rmse', ''),
                                    'Time_Horizon': horizon_name,
                                    'Type': horizon_pred.get('type', 'forecast')
                                })
            
            if horizon_data:
                df = pd.DataFrame(horizon_data)
                csv_filename = f'{filename}_{horizon_name}.csv'
                df.to_csv(csv_filename, index=False)
                exported_files.append(csv_filename)
                print(f"✅ Exported {len(horizon_data)} records to {csv_filename}")
        
        # Export summary report
        summary_data = []
        for item_pred in report['predictions']:
            item = item_pred['item']
            row = {level_name: item, 'Data_Points': item_pred['data_points']}
            
            for horizon_name in time_horizons.keys():
                horizon_pred = item_pred['predictions'].get(horizon_name)
                if horizon_pred and horizon_pred['summary']:
                    summary = horizon_pred['summary']
                    
                    # Handle multiple models in summary
                    if isinstance(summary, dict) and len(summary) > 0:
                        # Get the first model's summary for overall statistics
                        first_model = list(summary.keys())[0]
                        model_summary = summary[first_model]
                        
                        row.update({
                            f'{horizon_name}_Total': model_summary['total_predicted'],
                            f'{horizon_name}_Daily_Avg': model_summary['daily_average'],
                            f'{horizon_name}_Trend': model_summary['trend']
                        })
                    else:
                        # Single model summary (legacy format)
                        row.update({
                            f'{horizon_name}_Total': summary.get('total_predicted', None),
                            f'{horizon_name}_Daily_Avg': summary.get('daily_average', None),
                            f'{horizon_name}_Trend': summary.get('trend', None)
                        })
                    
                    # Add metrics if available
                    if horizon_pred.get('metrics'):
                        # Get average metrics across all models
                        all_mae = [m.get('mae', 0) for m in horizon_pred['metrics'].values() if m]
                        all_mape = [m.get('mape', 0) for m in horizon_pred['metrics'].values() if m]
                        all_rmse = [m.get('rmse', 0) for m in horizon_pred['metrics'].values() if m]
                        
                        row.update({
                            f'{horizon_name}_MAE': np.mean(all_mae) if all_mae else None,
                            f'{horizon_name}_MAPE': np.mean(all_mape) if all_mape else None,
                            f'{horizon_name}_RMSE': np.mean(all_rmse) if all_rmse else None
                        })
                    else:
                        row.update({
                            f'{horizon_name}_MAE': None,
                            f'{horizon_name}_MAPE': None,
                            f'{horizon_name}_RMSE': None
                        })
                else:
                    row.update({
                        f'{horizon_name}_Total': None,
                        f'{horizon_name}_Daily_Avg': None,
                        f'{horizon_name}_Trend': None,
                        f'{horizon_name}_MAE': None,
                        f'{horizon_name}_MAPE': None,
                        f'{horizon_name}_RMSE': None
                    })
            
            summary_data.append(row)
        
        if summary_data:
            df_summary = pd.DataFrame(summary_data)
            summary_filename = f'{filename}_summary.csv'
            df_summary.to_csv(summary_filename, index=False)
            exported_files.append(summary_filename)
            print(f"✅ Exported summary to {summary_filename}")
        
        print(f"\n🎉 EXPORT COMPLETE!")
        print(f"📁 Files exported: {len(exported_files)}")
        for file in exported_files:
            print(f"  📄 {file}")
        
        return exported_files

    def _compute_historical_windows(self, df, target_col='qtton'):
        """Compute last 10 days, last 10 weeks, last 10 fortnights historical windows.
        Returns a dict with three DataFrames: days, weeks, fortnights.
        """
        if df.empty or 'date_short' not in df.columns or target_col not in df.columns:
            return {
                'last_10_days': pd.DataFrame(),
                'last_10_weeks': pd.DataFrame(),
                'last_10_fortnights': pd.DataFrame()
            }
        data = df.copy()
        data['date_short'] = pd.to_datetime(data['date_short'], errors='coerce')
        data = data.dropna(subset=['date_short', target_col])
        if data.empty:
            return {
                'last_10_days': pd.DataFrame(),
                'last_10_weeks': pd.DataFrame(),
                'last_10_fortnights': pd.DataFrame()
            }
        data = data.sort_values('date_short')

        # Last 10 days - daily values of the most recent 10 dates
        last_10_days_df = data.tail(10)[['date_short', target_col]].copy()
        last_10_days_df = last_10_days_df.rename(columns={'date_short': 'date', target_col: 'value'})

        # Last 10 weeks - aggregate to weekly (ISO week) and take last 10
        weekly = data.set_index('date_short').resample('W')[target_col].sum().reset_index()
        weekly = weekly.rename(columns={'date_short': 'period_end', target_col: 'total'})
        weekly['period_start'] = weekly['period_end'] - pd.to_timedelta(6, unit='D')
        last_10_weeks_df = weekly.tail(10)[['period_start', 'period_end', 'total']]

        # Last 10 fortnights (14-day windows) - rolling 14-day sums by calendar periods
        # Build non-overlapping 14-day bins from the latest day backward
        dates = data['date_short'].tolist()
        values = data[target_col].tolist()
        if len(dates) > 0:
            end_date = pd.to_datetime(dates[-1]).normalize()
        else:
            end_date = pd.Timestamp.today().normalize()
        periods = []
        for i in range(10):
            p_end = end_date - pd.to_timedelta(14 * i, unit='D')
            p_start = p_end - pd.to_timedelta(13, unit='D')
            mask = (data['date_short'] >= p_start) & (data['date_short'] <= p_end)
            total = data.loc[mask, target_col].sum()
            periods.append({'period_start': p_start, 'period_end': p_end, 'total': total})
        last_10_fortnights_df = pd.DataFrame(periods).sort_values('period_start')

        return {
            'last_10_days': last_10_days_df.reset_index(drop=True),
            'last_10_weeks': last_10_weeks_df.reset_index(drop=True),
            'last_10_fortnights': last_10_fortnights_df.reset_index(drop=True)
        }

    def _limit_historical_data_for_forecast(self, data, target_col='qtton', periods=14):
        """Limit historical data based on forecast period:
        - 1 day forecast: use last 10 days
        - 1 week (7 days) forecast: use last 10 weeks  
        - 2 weeks (14 days) forecast: use last 10 fortnights
        """
        if data.empty or 'date_short' not in data.columns or target_col not in data.columns:
            return data
            
        data = data.copy()
        data['date_short'] = pd.to_datetime(data['date_short'], errors='coerce')
        data = data.dropna(subset=['date_short', target_col])
        data = data.sort_values('date_short')
        
        if data.empty:
            return data
            
        if periods == 1:
            # 1 day forecast: use last 10 days
            print("📅 Using last 10 days for 1-day forecast")
            return data.tail(10)
        elif periods == 7:
            # 1 week forecast: use last 10 weeks (70 days)
            print("📅 Using last 10 weeks for 1-week forecast")
            return data.tail(70)
        elif periods == 14:
            # 2 weeks forecast: use last 10 fortnights (140 days)
            print("📅 Using last 10 fortnights for 2-week forecast")
            return data.tail(140)
        else:
            # For other periods, use a reasonable amount based on the period
            historical_days = min(periods * 10, len(data))
            print(f"📅 Using last {historical_days} days for {periods}-day forecast")
            return data.tail(historical_days)

    def generate_historical_baseline_report(self, target_col='qtton', level='sku'):
        """Generate historical data used to predict: last 10 days, weeks, fortnights for all items."""
        print("\n📊 Generating historical baseline report")
        if level.lower() == 'family':
            items = self.get_available_families()
            item_type = 'family'
        else:
            items = self.get_available_skus()
            item_type = 'sku'

        results = {
            'metadata': {
                'generated_at': datetime.now().isoformat(),
                'level': level,
                'target_col': target_col,
                'total_items': len(items)
            },
            'days': [],
            'weeks': [],
            'fortnights': []
        }

        for item in items:
            try:
                if level.lower() == 'family':
                    df = self.get_family_data(item)
                    if not df.empty:
                        df = self.aggregate_family_data(df, target_col)
                        # aggregated returns columns: date_short, target_col, family
                        # Ensure item label sticks
                        item_label = item
                else:
                    df = self.get_sku_data(item)
                    item_label = item

                windows = self._compute_historical_windows(df, target_col)
                # Append with item label
                if not windows['last_10_days'].empty:
                    tmp = windows['last_10_days'].copy()
                    tmp[item_type.upper()] = item_label
                    results['days'].append(tmp)
                if not windows['last_10_weeks'].empty:
                    tmp = windows['last_10_weeks'].copy()
                    tmp[item_type.upper()] = item_label
                    results['weeks'].append(tmp)
                if not windows['last_10_fortnights'].empty:
                    tmp = windows['last_10_fortnights'].copy()
                    tmp[item_type.upper()] = item_label
                    results['fortnights'].append(tmp)
            except Exception as e:
                print(f"  ❌ Failed computing windows for {item_type} {item}: {e}")
                continue

        # Concatenate
        results['days'] = pd.concat(results['days'], ignore_index=True) if results['days'] else pd.DataFrame()
        results['weeks'] = pd.concat(results['weeks'], ignore_index=True) if results['weeks'] else pd.DataFrame()
        results['fortnights'] = pd.concat(results['fortnights'], ignore_index=True) if results['fortnights'] else pd.DataFrame()

        print("✅ Historical baseline report ready")
        return results

    def export_historical_baselines_to_csv(self, hist_report, filename_prefix=None):
        """Export historical windows to three CSVs: days, weeks, fortnights."""
        if not hist_report:
            return []
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        level = hist_report['metadata'].get('level', 'sku')
        base = filename_prefix or f"{level}_historical_baselines_{ts}"
        exported = []
        if isinstance(hist_report.get('days'), pd.DataFrame) and not hist_report['days'].empty:
            f = f"{base}_last_10_days.csv"
            hist_report['days'].to_csv(f, index=False)
            exported.append(f)
        if isinstance(hist_report.get('weeks'), pd.DataFrame) and not hist_report['weeks'].empty:
            f = f"{base}_last_10_weeks.csv"
            hist_report['weeks'].to_csv(f, index=False)
            exported.append(f)
        if isinstance(hist_report.get('fortnights'), pd.DataFrame) and not hist_report['fortnights'].empty:
            f = f"{base}_last_10_fortnights.csv"
            hist_report['fortnights'].to_csv(f, index=False)
            exported.append(f)
        print(f"📁 Exported {len(exported)} historical CSVs")
        return exported
    
    def get_raw_material_data(self):
        """Get raw material formula data"""
        try:
            conn = self.get_db_connection()
            
            # Try different table names
            table_names = ['%skuformula%','%skuformula_2%'] #HARDCODE 'sku_formula', 'skuformula', 'formula']
            for table_name in table_names:
                try:
                    query = f"SELECT * FROM {table_name}"
                    df = pd.read_sql_query(query, conn)
                    if not df.empty:
                        conn.close()
                        return df
                except:
                    continue
            
            # If no data found, try to load from CSV
            csv_file = 'data/SKUFormula 2.csv' #HARDCODE
            if os.path.exists(csv_file):
                print(f"📁 Loading formula data from CSV: {csv_file}")
                df = pd.read_csv(csv_file)
                conn.close()
                return df
            
            conn.close()
            return pd.DataFrame()
            
        except Exception as e:
            print(f"❌ Error getting raw material data: {e}")
            return pd.DataFrame()
    
    def get_formula_data(self):
        """Get formula data with raw material details"""
        try:
            conn = self.get_db_connection()
            
            # Try different table names
            table_names = ['formula%','%formula_2%']# HARDCODE 'formula', 'formulas']
            for table_name in table_names:
                try:
                    query = f"SELECT * FROM {table_name}"
                    df = pd.read_sql_query(query, conn)
                    if not df.empty:
                        conn.close()
                        return df
                except:
                    continue
            
            # If no data found, try to load from CSV
            csv_file = 'data/Formula 2.csv' #HARDCODE 'Formula.csv'
            if os.path.exists(csv_file):
                print(f"📁 Loading formula data from CSV: {csv_file}")
                df = pd.read_csv(csv_file)
                conn.close()
                return df
            
            conn.close()
            return pd.DataFrame()
            
        except Exception as e:
            print(f"❌ Error getting formula data: {e}")
            return pd.DataFrame()
    
    def calculate_raw_material_forecast(self, sku_forecast, selected_sku, target_col='qtton'):
        """Calculate raw material requirements based on SKU forecast and selected SKU"""
        print(f"\n🏭 CALCULATING RAW MATERIAL REQUIREMENTS FOR SKU: {selected_sku}")
        print("=" * 60)
        
        # Get raw material data
        sku_formula_df = self.get_raw_material_data()
        formula_df = self.get_formula_data()
        
        if sku_formula_df.empty or formula_df.empty:
            print("⚠️  No raw material data found")
            return None
        
        # Clean column names
        sku_formula_df.columns = sku_formula_df.columns.str.strip().str.lower().str.replace(' ', '_')
        formula_df.columns = formula_df.columns.str.strip().str.lower().str.replace(' ', '_')
        
        print(f"📊 SKU Formula records: {len(sku_formula_df)}")
        print(f"📊 Formula records: {len(formula_df)}")
        
        # Use the selected SKU instead of getting from forecast
        sku = selected_sku
        
        # Find the formula for this specific SKU
        sku_formulas = sku_formula_df[sku_formula_df['sku'] == sku] if 'sku' in sku_formula_df.columns else sku_formula_df
        
        if sku_formulas.empty:
            print(f"⚠️  No formula found for SKU: {sku}")
            return None
        
        # Get the SKU formula name
        sku_formula_name = sku_formulas['sku_formula'].iloc[0] if 'sku_formula' in sku_formulas.columns else sku_formulas.iloc[0, 0]
        
        # Find matching formula data for this specific SKU formula
        formula_data = formula_df[formula_df['sku_formula'] == sku_formula_name] if 'sku_formula' in formula_df.columns else formula_df
        
        if formula_data.empty:
            print(f"⚠️  No formula data found for: {sku_formula_name}")
            return None
        
        print(f"🎯 Processing SKU: {sku}")
        print(f"📋 Formula: {sku_formula_name}")
        print(f"🧪 Raw materials: {len(formula_data)}")
        
        # Calculate raw material requirements
        raw_material_forecast = []
        
        for _, formula_row in formula_data.iterrows():
            raw_material = formula_row.get('sku_raw', formula_row.get('raw_name', 'Unknown'))
            raw_name = formula_row.get('raw_name', raw_material)
            ton_percentage = formula_row.get('ton', 0)
            
            # Calculate required amount for each forecast period
            forecast_requirements = []
            
            for forecast_day in sku_forecast['forecasts']:
                forecast_date = forecast_day['date']
                forecast_qty = forecast_day['mean']  # Use mean forecast
                
                # Calculate raw material requirement
                required_amount = forecast_qty * (ton_percentage / 100)
                
                forecast_requirements.append({
                    'date': forecast_date,
                    'sku': sku,
                    'raw_material': raw_material,
                    'raw_name': raw_name,
                    'sku_forecast': forecast_qty,
                    'ton_percentage': ton_percentage,
                    'required_amount': required_amount
                })
            
            raw_material_forecast.extend(forecast_requirements)
        
        # Create summary
        total_requirements = {}
        for req in raw_material_forecast:
            raw_mat = req['raw_material']
            if raw_mat not in total_requirements:
                total_requirements[raw_mat] = {
                    'raw_name': req['raw_name'],
                    'total_amount': 0,
                    'ton_percentage': req['ton_percentage']
                }
            total_requirements[raw_mat]['total_amount'] += req['required_amount']
        
        results = {
            'sku': sku,
            'sku_formula': sku_formula_name,
            'raw_material_forecast': raw_material_forecast,
            'summary': total_requirements,
            'total_raw_materials': len(total_requirements),
            'forecast_period': len(sku_forecast['forecasts'])
        }
        
        print(f"✅ Raw material calculation complete!")
        print(f"📊 Total raw materials: {results['total_raw_materials']}")
        for raw_mat, data in total_requirements.items():
            print(f"  🧪 {data['raw_name']}: {data['total_amount']:.2f} tons")
        
        return results
    
    def setup_dashboard(self):
        """Setup the Dash dashboard"""
        print("📈 Setting up Monte Carlo Dashboard...")
        
        self.app.layout = dbc.Container([
            # Header
            dbc.Row([
                dbc.Col([
                    html.H1("🤖 Multi-Model Forecasting Dashboard", className="text-center mb-4"),
                    html.P("Monte Carlo, ARIMA, Linear Regression, Ridge, Lasso, Random Forest", # removed Xgboost
                           className="text-center text-muted mb-4")
                ])
            ]),
            
            # Controls
            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader("🎯 Forecasting Controls"),
                        dbc.CardBody([
                            dbc.Row([
                                dbc.Col([
                                    html.Label("Select SKU:"),
                                    dcc.Dropdown(
                                        id='sku-dropdown',
                                        placeholder="Choose SKU for analysis",
                                        multi=False
                                    )
                                ], width=3),
                                dbc.Col([
                                    html.Label("Target Metric:"),
                                    dcc.Dropdown(
                                        id='metric-dropdown',
                                        options=[
                                            {'label': 'QTTON', 'value': 'qtton'},
                                            {'label': 'QTSAC', 'value': 'qtsac'}
                                        ],
                                        value='qtton'
                                    )
                                ], width=3),
                                dbc.Col([
                                    html.Label("Forecasting Model:"),
                                    dcc.Dropdown(
                                        id='model-dropdown',
                                        options=[
                                            {'label': '🎲 Monte Carlo', 'value': 'monte_carlo'},
                                            {'label': '📈 ARIMA', 'value': 'arima'},
                                            {'label': '📉 Linear Regression', 'value': 'linear_regression'},
                                            {'label': '🏔️ Ridge Regression', 'value': 'ridge_regression'},
                                            {'label': '🎯 Lasso Regression', 'value': 'lasso_regression'},
                                            {'label': '🌲 Random Forest', 'value': 'random_forest'},
                                            #{'label': '⚡ XGBoost', 'value': 'xgboost'},
                                            {'label': '🤖 All Models Comparison', 'value': 'all_models'}
                                        ],
                                        value='monte_carlo'
                                    )
                                ], width=3),
                                dbc.Col([
                                    html.Label("Forecast Period:"),
                                    dcc.Dropdown(
                                        id='period-dropdown',
                                        options=[
                                            {'label': '1 Day', 'value': 1},
                                            {'label': '1 Week', 'value': 7},
                                            {'label': '2 Weeks', 'value': 14},
                                            {'label': '1 Month', 'value': 30}
                                        ],
                                        value=14
                                    )
                                ], width=3)
                            ]),
                            html.Br(),
                            dbc.Row([
                                dbc.Col([
                                    html.Label("Start Date:"),
                                    dcc.DatePickerSingle(
                                        id='start-date-picker',
                                        date=datetime.now().date(),
                                        display_format='YYYY-MM-DD'
                                    )
                                ], width=4),
                                dbc.Col([
                                    html.Label("Error Reduction:"),
                                    dcc.Checklist(
                                        id='error-reduction-checkbox',
                                        options=[{'label': 'Enable Error Reduction', 'value': 'enabled'}],
                                        value=['enabled'],
                                        inline=True
                                    ),
                                    html.Br(),
                                    html.Label("Actions:"),
                                    html.Br(),
                                    dbc.Button("🎲 Generate Forecast", id="forecast-btn", 
                                             color="success", className="me-2"),
                                    dbc.Button("📊 Overall Metrics", id="overall-btn", 
                                             color="info")
                                ], width=4)
                            ])
                        ])
                    ])
                ], width=12)
            ], className="mb-4"),
            
            # Status and Results
            dbc.Row([
                dbc.Col([
                    dbc.Alert(id="status-message", color="info")
                ], width=12)
            ], className="mb-4"),
            
            #Charts
            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader("📈 Model(s) Forecast"),
                        dbc.CardBody([
                            dcc.Graph(id='forecast-chart')
                        ])
                    ])
                ], width=8),
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader("📊 Seasonality & Peaks"),
                        dbc.CardBody([
                            dcc.Graph(id='analysis-chart')
                        ])
                    ])
                ], width=4)
            ], className="mb-4"),
            #--------------------NEW FEATURE------------------------
            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader([
                            html.H5("Comparison: Forecasts Trained on Different Time Windows", className="mb-1"),
                            html.Small("Same model(s), but trained only on last 3Months, 6Months, 12Months of data", className="text-muted")
                        ]),
                        dbc.CardBody([
                # Tabs for the different training periods
                            dcc.Tabs(
                                id='window-tabs-comparison',
                                value='3m',
                                children=[
                                    dcc.Tab(label='Last 3 Months', value='3m'),
                                    dcc.Tab(label='Last 6 Months', value='6m'),
                                    dcc.Tab(label='Last 12 Months', value='12m'),
                                ],
                            className="mb-3"
                            ),
                # New graph just for window comparison
                            dcc.Graph(
                            id='window-comparison-chart',
                            style={'height': '550px'}
                            )
                        ])
                    ], className="shadow mt-4 mb-5")
                ], width=12)
            ], className="mb-5"),
            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader("📊 Metrics per Training Window"),
                        dbc.CardBody([
                # Tabs to match the comparison graph
                            dcc.Tabs(
                                id='window-tabs-metrics',
                                value='3m',
                                children=[
                                    dcc.Tab(label='Last 3 Months', value='3m'),
                                    dcc.Tab(label='Last 6 Months', value='6m'),
                                    dcc.Tab(label='Last 12 Months', value='12m'),
                                ],
                            className="mb-3"
                            ),
                # Container for the metrics table
                        html.Div(id='window-metrics-container')
                        ])
                    ], className="shadow mt-4 mb-5")
                ], width=12)
            ], className="mb-5"),
            

            #dcc.Graph(id='multi-model-forecast-graph', style={'height': '550px'}),
                        
                        # Metrics
                        dbc.Row([
                            dbc.Col([
                                dbc.Card([
                                    dbc.CardHeader("📋 Metrics Table"),
                                    dbc.CardBody([
                                        html.Div(id="metrics-table")
                                    ])
                                ])
                            ], width=6),
                            dbc.Col([
                                dbc.Card([
                                    dbc.CardHeader("🎯 Overall Metrics (All SKUs)"),
                                    dbc.CardBody([
                                        html.Div(id="overall-metrics")
                                    ])
                                ])
                            ], width=6)
                        ], className="mb-4"),
                
            # Raw Material Forecasting
            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader([
                            "🏭 Raw Material Requirements",
                            html.Small(" (Updates automatically when SKU changes)", className="text-muted ms-2")
                        ]),
                        dbc.CardBody([
                            dbc.Row([
                                dbc.Col([
                                    dbc.Button("🏭 Calculate Raw Materials", id="raw-material-btn", 
                                             color="warning", className="me-2")
                                ], width=6),
                                dbc.Col([
                                    html.Div(id="raw-material-summary")
                                ], width=6)
                            ]),
                            html.Br(),
                            html.Div(id="raw-material-table")
                        ])
                    ])
                ], width=12)
            ], className="mb-4"),
            
            # Comprehensive Predictions Report
            dbc.Row([
                dbc.Col([
                    dbc.Card([
                        dbc.CardHeader([
                            "📊 Comprehensive Predictions Report",
                            html.Small(" (SKU/Family Level - Forward & Historical)", className="text-muted ms-2")
                        ]),
                        dbc.CardBody([
                            dbc.Row([
                                dbc.Col([
                                    html.Label("Analysis Level:"),
                                    dcc.Dropdown(
                                        id='report-level-dropdown',
                                        options=[
                                            {'label': 'SKU Level', 'value': 'sku'},
                                            {'label': 'Family Level', 'value': 'family'}
                                        ],
                                        value='sku'
                                    )
                                ], width=3),
                                dbc.Col([
                                    html.Label("Target Metric:"),
                                    dcc.Dropdown(
                                        id='report-metric-dropdown',
                                        options=[
                                            {'label': 'QTTON', 'value': 'qtton'},
                                            {'label': 'QTSAC', 'value': 'qtsac'}
                                        ],
                                        value='qtton'
                                    )
                                ], width=3),
                                dbc.Col([
                                    html.Label("Start Date:"),
                                    dcc.DatePickerSingle(
                                        id='report-start-date-picker',
                                        date=datetime.now().date(),
                                        display_format='YYYY-MM-DD'
                                    )
                                ], width=3),
                                dbc.Col([
                                    html.Label("Include Historical:"),
                                    dcc.Checklist(
                                        id='include-historical-checkbox',
                                        options=[{'label': 'Include Last 10 Instances', 'value': 'enabled'}],
                                        value=['enabled'],
                                        inline=True
                                    ),
                                    html.Br(),
                                    html.Label("Use All Models:"),
                                    dcc.Checklist(
                                        id='use-all-models-checkbox',
                                        options=[{'label': 'Compare All Models', 'value': 'enabled'}],
                                        value=['enabled'],
                                        inline=True
                                    ),
                                    html.Br(),
                                    html.Label("Actions:"),
                                    html.Br(),
                                    dbc.Button("📊 Generate Report", id="generate-report-btn", 
                                             color="primary", className="me-2"),
                                    dbc.Button("📁 Export CSV", id="export-csv-btn", 
                                             color="success", disabled=True),
                                    html.Br(),
                                    html.Br(),
                                    dbc.Button("🕘 Export Historical Baselines", id="export-hist-btn", 
                                             color="secondary")
                                ], width=3)
                            ]),
                            html.Br(),
                            dbc.Alert(id="report-status-message", color="info"),
                            html.Div(id="report-summary"),
                            html.Div(id="report-table")
                        ])
                    ])
                ], width=12)
            ], className="mb-4")
        ], fluid=True)
        
        # Callbacks
        @self.app.callback(
            Output('sku-dropdown', 'options'),
            Input('forecast-btn', 'n_clicks')
        )
        def update_sku_options(n_clicks):
            skus = self.get_available_skus()
            return [{'label': f'SKU: {sku}', 'value': sku} for sku in skus]
        
        @self.app.callback(
            [Output('status-message', 'children'),
             Output('forecast-chart', 'figure'),
             Output('analysis-chart', 'figure'),
             Output('metrics-table', 'children')],
            [Input('forecast-btn', 'n_clicks'),
             Input('sku-dropdown', 'value'),
             Input('model-dropdown', 'value'),
             Input('metric-dropdown', 'value'),
             Input('period-dropdown', 'value'),
             Input('start-date-picker', 'date'),
             Input('error-reduction-checkbox', 'value')]
        )
        def update_forecast(forecast_clicks, selected_sku, selected_model, selected_metric, 
                           selected_periods, selected_start_date, error_reduction_enabled):
            ctx = callback_context
            if not selected_sku or not ctx.triggered:
                empty_fig = go.Figure()
                empty_fig.add_annotation(text="Please select a SKU and generate forecast", 
                                       x=0.5, y=0.5, showarrow=False)
                return "Select a SKU and click 'Generate Forecast'", empty_fig, empty_fig, "No metrics available"
            if ctx.triggered[0]['prop_id'] == 'forecast-btn.n_clicks':
                try:
                    # Get SKU data
                    sku_data = self.get_sku_data(selected_sku)
                    if sku_data.empty:
                        return "❌ No data found for selected SKU", go.Figure(), go.Figure(), "No data"
                    # Generate forecast based on selected model
                    start_date = datetime.strptime(selected_start_date, '%Y-%m-%d').date()
                    error_reduction = 'enabled' in (error_reduction_enabled or [])
                    if selected_model == 'all_models':
                        # Run all models comparison
                        forecast_result = self.compare_all_models(
                            sku_data, selected_metric, selected_periods, start_date
                        )
                        if not forecast_result:
                            return "❌ Failed to generate forecasts", go.Figure(), go.Figure(), "Forecast failed"
                        # Create comparison chart
                        forecast_fig = self.create_model_comparison_chart(forecast_result, selected_metric)
                        analysis_fig = self.create_model_comparison_analysis(forecast_result)
                        metrics_table = self.create_model_comparison_metrics(forecast_result, sku_data, selected_metric)
                        status_msg = f"✅ All models comparison generated for SKU {selected_sku} - {selected_periods} days"
                    else:
                        # Run single model
                        forecast_result = self.run_single_model(
                            selected_model, sku_data, selected_metric, selected_periods, start_date, error_reduction
                        )
                        if forecast_result is None:
                            return "❌ Failed to generate forecast", go.Figure(), go.Figure(), "Forecast failed"
                        # Create forecast chart
                        forecast_fig = self.create_forecast_chart(forecast_result, selected_metric)
                        # Create analysis chart
                        analysis_fig = self.create_analysis_chart(forecast_result)
                        # Create metrics table
                        metrics_table = self.create_metrics_table(forecast_result, sku_data, selected_metric)
                        model_name = forecast_result.get('model_name', selected_model)
                        status_msg = f"✅ {model_name} forecast generated for SKU {selected_sku} - {selected_periods} days"
                    return status_msg, forecast_fig, analysis_fig, metrics_table
                except Exception as e:
                    return f"❌ Error: {e}", go.Figure(), go.Figure(), "Error occurred"
            return "Click 'Generate Forecast' to start", go.Figure(), go.Figure(), "No forecast generated"

#------------------ NEW FEATURE: Windowed Comparison Chart -----------------------------        

        @self.app.callback(
            Output('window-comparison-chart', 'figure'),
            [Input('forecast-btn', 'n_clicks'),
            Input('window-tabs-comparison', 'value'),
            Input('sku-dropdown', 'value'),
            Input('model-dropdown', 'value'),
            Input('metric-dropdown', 'value'),
            Input('period-dropdown', 'value'),
            Input('start-date-picker', 'date'),
            Input('error-reduction-checkbox', 'value')],
            prevent_initial_call=True
        )
        def update_window_comparison_chart(forecast_clicks, active_tab, selected_sku, selected_model, selected_metric,
                                        selected_periods, selected_start_date, error_reduction_enabled):
            
            if not selected_sku or not forecast_clicks:
                fig = go.Figure()
                fig.add_annotation(text="Generate forecast first", x=0.5, y=0.5, showarrow=False)
                return fig

            try:
                start_date = datetime.strptime(selected_start_date, '%Y-%m-%d').date()
                error_reduction = 'enabled' in (error_reduction_enabled or [])
                horizon_days = int(selected_periods)

                sku_data = self.get_sku_data(selected_sku)
                if sku_data.empty:
                    return go.Figure()

                fig = go.Figure()

                # Loop over each training window
                for w in TRAINING_WINDOWS:
                    w_value = w['value']
                    w_label = w['label']
                    months = w['months']

                    window_data = self.filter_by_training_window(sku_data, months)
                    if window_data is None or len(window_data) < 10:
                        continue

                    # Run the forecast function
                    if selected_model == 'all_models':
                        results = self.compare_all_models(
                            window_data, selected_metric, horizon_days, start_date
                        )
                        # results is now dict: {'Monte Carlo': {...}, 'ARIMA': {...}, ...}
                    else:
                        single_result = self.run_single_model(
                            selected_model, window_data, selected_metric, horizon_days,
                            start_date, error_reduction
                        )
                        results = {selected_model: single_result} if single_result else {}

                    # Now loop over each model result in this window
                    for model_name, result in results.items():
                        if not result or not isinstance(result, dict):
                            continue

                        # Extract predictions (same as Monte Carlo)
                        preds = [day['mean'] for day in result.get('forecasts', [])]
                        if not preds:
                            continue

                        dates = result.get('forecast_dates', 
                                        pd.date_range(start=start_date, periods=horizon_days, freq='D'))

                        fig.add_trace(go.Scatter(
                            x=dates,
                            y=preds,
                            mode='markers' if len(dates)==1 else 'lines',
                            name=f"{model_name.title()} – {w_label}",
                            line=dict(
                                width=3.5 if model_name.lower() == 'monte carlo' else 2.5,
                                dash='dash' if w_value != active_tab else 'solid'
                            ),
                            visible=(w_value == active_tab) or 'legendonly'
                        ))

                fig.update_layout(
                    #title=f"Training Window Comparison – {selected_sku} ({selected_metric})",
                    xaxis_title="Forecast Date",
                    yaxis_title=selected_metric.upper(),
                    hovermode="x unified",
                    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                    height=550,
                    margin=dict(l=50, r=30, t=80, b=60)
                )

                return fig

            except Exception as e:
                fig = go.Figure()
                fig.add_annotation(text=f"Error: {str(e)}", x=0.5, y=0.5, showarrow=False)
                return fig
#----------------------------------------------------------------------------------------------------------------------------        
        @self.app.callback(
                Output('overall-metrics', 'children'),
                Input('overall-btn', 'n_clicks')
            )
        def update_overall_metrics(n_clicks):
                if n_clicks:
                    try:
                        results = self.get_all_skus_metrics()
                        if results:
                            return self.create_overall_metrics_display(results)
                        else:
                            return "❌ Failed to calculate overall metrics"
                    except Exception as e:
                        return f"❌ Error: {e}"
                
                return "Click 'Overall Metrics' to calculate metrics for all SKUs"
            
#------------------------------ NEW FEATURE: Windowed Metrics------------------------------------
        @self.app.callback(
            Output('window-metrics-container', 'children'),
            [Input('forecast-btn', 'n_clicks'),
            Input('window-tabs-metrics', 'value'),
            Input('sku-dropdown', 'value'),
            Input('model-dropdown', 'value'),
            Input('metric-dropdown', 'value'),
            Input('period-dropdown', 'value'),
            Input('start-date-picker', 'date'),
            Input('error-reduction-checkbox', 'value')],
            prevent_initial_call=True
        )
        def update_window_metrics(n_clicks, active_tab, sku, model_choice, metric, periods, start_date_str, error_checklist):
            
            if not n_clicks or not sku:
                return html.Div("Generate forecast first", className="text-muted")

            try:
                start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
                error_reduction = 'enabled' in (error_checklist or [])
                horizon_days = int(periods)

                full_data = self.get_sku_data(sku)
                if full_data.empty:
                    return html.Div("No data available", className="text-danger")

                # Same logic as comparison chart: run per window
                if model_choice == 'all_models':
                    forecast_func = self.compare_all_models
                    func_args = (full_data, metric, horizon_days, start_date)
                else:
                    forecast_func = self.run_single_model
                    func_args = (model_choice, full_data, metric, horizon_days, start_date, error_reduction)

                # We only need the active tab's result for this callback
                # But to keep it simple, we can re-run for the active window only
                for w in TRAINING_WINDOWS:
                    if w['value'] == active_tab:
                        months = w['months']
                        w_label = w['label']
                        
                        window_data = self.filter_by_training_window(full_data, months)
                        if window_data is None or len(window_data) < 10:
                            return html.Div([
                                html.H6(f"Metrics for {w_label}", className="text-center mb-3"),
                                html.Div("Insufficient data for this window", className="text-warning")
                            ])

                        # Run the forecast for this window
                        if model_choice == 'all_models':
                            result = forecast_func(window_data, metric, horizon_days, start_date)
                        else:
                            result = forecast_func(model_choice, window_data, metric, horizon_days, start_date, error_reduction)

                        # if not result or 'forecasts' not in result or not result['forecasts']:
                        #     return html.Div([
                        #             html.H6(f"Metrics for {w_label}", className="text-center mb-3"),
                        #             html.Div("No valid forecast data for this window", className="text-warning")
                        #             ])

                        # Create metrics table (your existing function)
                        # For all_models, you might need to pick the best or show a summary
#                         if model_choice == 'all_models':
#                             # Get all model results for this window
#                             all_window_results = result  # since compare_all_models returns dict of models

#                             if not all_window_results:
#                                 return html.Div(f"No model results for {w_label}", className="text-danger")

#                             # Find the best model by lowest MAE
#                             best_model_name = None
#                             best_mae = float('inf')
#                             best_result = None

#                             for m_name, m_result in all_window_results.items():
#                                 # Assuming your create_metrics_table can compute MAE internally
#                                 # OR you have a helper to compute metrics from result + historical data
#                                 metrics = self.compute_metrics_from_result(m_result, window_data, metric)  # ← new helper, see below
#                                 current_mae = metrics.get('MAE', float('inf'))
#                                 if current_mae < best_mae:
#                                     best_mae = current_mae
#                                     best_model_name = m_name
#                                     best_result = m_result

#                             if best_result is None:
#                                 return html.Div(f"No valid metrics for {w_label}", className="text-warning")

# ################ Best model Calculation#############################
#                             best_model_name = None
#                             best_metrics = None
#                             best_result = None
#                             best_rmse = float('inf')

#                             for model_name, model_result in result.items():
#                                 metrics = self.compute_forecast_metrics(model_result, window_data, metric)
#                                 if not metrics:
#                                     continue

#                                 if metrics['rmse'] < best_rmse:
#                                     best_rmse = metrics['rmse']
#                                     best_model_name = model_name
#                                     best_metrics = metrics
#                                     best_result = model_result

#                             if best_result is None:
#                                 return html.Div(f"No valid metrics for {w_label}", className="text-warning")

#                             table = self.create_metrics_table(best_result, window_data, metric)
#                             title = f"Metrics for {w_label} – Best Model: {best_model_name} (RMSE: {best_rmse:.2f})"
                            
#                             return html.Div([
#                                 html.H6(title, className="text-center mb-3"),
#                                 table
#                             ])
                        
#                         elif model_choice != 'all_models':
#                             table = self.create_metrics_table(result, window_data, metric)
#                             title = f"Metrics for {w_label} – Model: {result.get('model_name', model_choice)}"

#                             return html.Div([
#                                 html.H6(title, className="text-center mb-3"),
#                                 table
#                             ])

#                 return html.Div("No data for selected window", className="text-muted")

#             except Exception as e:
#                 print(f"Metrics error: {str(e)}")
#                 return html.Div(f"Error: {str(e)}", className="text-danger")
                        # ---------------- SINGLE MODEL ----------------
                        if model_choice != 'all_models':
                            if not result or 'forecasts' not in result or not result['forecasts']:
                                return html.Div([
                                    html.H6(f"Metrics for {w_label}", className="text-center mb-3"),
                                    html.Div("No valid forecast data for this window", className="text-warning")
                                ])

                            table = self.create_metrics_table(result, window_data, metric)
                            title = f"Metrics for {w_label} – Model: {result.get('model_name', model_choice)}"

                            return html.Div([
                                html.H6(title, className="text-center mb-3"),
                                table
                            ])


                        # ---------------- ALL MODELS ----------------
                        if not isinstance(result, dict) or not result:
                            return html.Div([
                                html.H6(f"Metrics for {w_label}", className="text-center mb-3"),
                                html.Div("No model results for this window", className="text-warning")
                            ])

                        best_model_name = None
                        best_result = None
                        best_rmse = float('inf')

                        for model_name, model_result in result.items():
                            if not model_result or 'forecasts' not in model_result:
                                continue

                            metrics = self.compute_forecast_metrics(model_result, window_data, metric)
                            if not metrics or 'rmse' not in metrics:
                                continue

                            if metrics['mape'] < best_rmse: #rmse → mape
                                best_rmse = metrics['mape'] #rmse → mape
                                best_model_name = model_name
                                best_result = model_result

                        if best_result is None:
                            return html.Div([
                                html.H6(f"Metrics for {w_label}", className="text-center mb-3"),
                                html.Div("No valid metrics for this window", className="text-warning")
                            ])

                        table = self.create_metrics_table(best_result, window_data, metric)
                        title = f"Metrics for {w_label} – Best Model: {best_model_name} (MAPE: {best_rmse:.2f})" #rmse → mape

                        return html.Div([
                            html.H6(title, className="text-center mb-3"),
                            table
                        ])

            except Exception as e:
                print(f"Metrics error: {str(e)}")
                return html.Div(f"Error: {str(e)}", className="text-danger")
#-------------------------------------------------------------------------------        
        @self.app.callback(
            [Output('raw-material-summary', 'children'),
             Output('raw-material-table', 'children')],
            [Input('raw-material-btn', 'n_clicks'),
             Input('sku-dropdown', 'value'),
             Input('metric-dropdown', 'value'),
             Input('period-dropdown', 'value'),
             Input('start-date-picker', 'date'),
             Input('forecast-btn', 'n_clicks')]
        )
        def update_raw_materials(raw_material_clicks, selected_sku, selected_metric, 
                                selected_periods, selected_start_date, forecast_clicks):
            ctx = callback_context
            
            # Update raw materials when SKU is selected
            if selected_sku:
                if ctx.triggered:
                    trigger_id = ctx.triggered[0]['prop_id']
                    print(f"🔍 Raw materials trigger: {trigger_id} for SKU: {selected_sku}")
                else:
                    print(f"🔍 Raw materials initial load for SKU: {selected_sku}")
                
                # Always update when SKU is selected
                try:
                    # Get SKU data and generate forecast first
                    sku_data = self.get_sku_data(selected_sku)
                    if sku_data.empty:
                        return "❌ No data found for selected SKU", "No data available"
                    
                    # Generate forecast
                    start_date = datetime.strptime(selected_start_date, '%Y-%m-%d').date()
                    forecast_result = self.monte_carlo_forecast(
                        sku_data, selected_metric, selected_periods, start_date
                    )
                    
                    if forecast_result is None:
                        return "❌ Failed to generate forecast", "Forecast failed"
                    
                    # Calculate raw material requirements
                    raw_material_result = self.calculate_raw_material_forecast(forecast_result, selected_sku, selected_metric)
                    
                    if raw_material_result is None:
                        return "⚠️ No raw material data available", "Raw material calculation failed"
                    
                    # Create summary display
                    summary = self.create_raw_material_summary(raw_material_result)
                    
                    # Create detailed table
                    table = self.create_raw_material_table(raw_material_result)
                    
                    return summary, table
                    
                except Exception as e:
                    return f"❌ Error: {e}", "Error occurred"
            
            # Default message when no SKU is selected
            return "Select a SKU to view raw materials", "No SKU selected"
        
        @self.app.callback(
            [Output('report-status-message', 'children'),
             Output('report-summary', 'children'),
             Output('report-table', 'children'),
             Output('export-csv-btn', 'disabled')],
            [Input('generate-report-btn', 'n_clicks'),
             Input('report-level-dropdown', 'value'),
             Input('report-metric-dropdown', 'value'),
             Input('report-start-date-picker', 'date'),
             Input('include-historical-checkbox', 'value'),
             Input('use-all-models-checkbox', 'value')]
        )
        def update_comprehensive_report(report_clicks, selected_level, selected_metric, selected_start_date, include_historical, use_all_models):
            ctx = callback_context
            
            if not report_clicks or not ctx.triggered:
                return "Click 'Generate Report' to create comprehensive predictions report", "", "", True
            
            if ctx.triggered[0]['prop_id'] == 'generate-report-btn.n_clicks':
                try:
                    # Generate comprehensive report
                    start_date = datetime.strptime(selected_start_date, '%Y-%m-%d').date()
                    include_historical_flag = 'enabled' in (include_historical or [])
                    use_all_models_flag = 'enabled' in (use_all_models or [])
                    report = self.generate_comprehensive_report(selected_metric, start_date, selected_level, include_historical_flag, use_all_models_flag)
                    
                    if report is None:
                        level_name = 'families' if selected_level == 'family' else 'SKUs'
                        return f"❌ Failed to generate report - no {level_name} found", "", "", True
                    
                    # Store report for CSV export
                    self.latest_report = report
                    
                    # Create summary display
                    summary_display = self.create_report_summary_display(report)
                    
                    # Create detailed table
                    table_display,table_data = self.create_report_table_display(report)

                    columns = list({col for row in table_data for col in row.keys()})
                    
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    with open(f'report_tables/report_table_{timestamp}.csv', mode='w', newline='', encoding='utf-8') as file:
                        writer = csv.DictWriter(file, fieldnames=columns)
                        writer.writeheader()
                        writer.writerows(table_data)
                    
                    metadata = report['metadata']
                    level_name = 'families' if selected_level == 'family' else 'SKUs'
                    horizon_count = len(metadata['time_horizons'])
                    model_info = f" with {len(metadata.get('model_names', ['Monte Carlo']))} models" if use_all_models_flag else " (Monte Carlo only)"
                    status_msg = f"✅ Report generated for {metadata['processed_items']} {level_name} across {horizon_count} time horizons{model_info}"
                    
                    return status_msg, summary_display, table_display, False
                    
                except Exception as e:
                    return f"❌ Error generating report: {e}", "", "", True
            
            return "Click 'Generate Report' to start", "", "", True
        
        @self.app.callback(
            Output('export-csv-btn', 'children'),
            [Input('export-csv-btn', 'n_clicks')]
        )
        def export_report_to_csv(export_clicks):
            ctx = callback_context
            
            if export_clicks and ctx.triggered[0]['prop_id'] == 'export-csv-btn.n_clicks':
                try:
                    if self.latest_report is None:
                        return "❌ No report available - generate report first"
                    
                    # Export the report to CSV
                    exported_files = self.export_predictions_to_csv(self.latest_report)
                    
                    if exported_files:
                        return f"✅ Exported {len(exported_files)} files"
                    else:
                        return "❌ Export failed"
                        
                except Exception as e:
                    return f"❌ Export failed: {e}"
            
            return "📁 Export CSV"

        @self.app.callback(
            Output('export-hist-btn', 'children'),
            [Input('export-hist-btn', 'n_clicks'),
             Input('report-level-dropdown', 'value'),
             Input('report-metric-dropdown', 'value')]
        )
        def export_historical_baselines(export_clicks, selected_level, selected_metric):
            ctx = callback_context
            if export_clicks and ctx.triggered[0]['prop_id'] == 'export-hist-btn.n_clicks':
                try:
                    hist_report = self.generate_historical_baseline_report(
                        target_col=selected_metric or 'qtton',
                        level=selected_level or 'sku'
                    )
                    files = self.export_historical_baselines_to_csv(hist_report)
                    if files:
                        return f"✅ Exported {len(files)} historical files"
                    return "❌ No data to export"
                except Exception as e:
                    return f"❌ Export failed: {e}"
            return "🕘 Export Historical Baselines"
    
    def create_forecast_chart(self, forecast_result, target_col):
        """Create forecast visualization"""
        forecasts = forecast_result['forecasts']
        dates = [f['date'] for f in forecasts]
        means = [f['mean'] for f in forecasts]
        p25 = [f['p25'] for f in forecasts]
        p75 = [f['p75'] for f in forecasts]
        mins = [f['min'] for f in forecasts]
        maxs = [f['max'] for f in forecasts]
        
        fig = go.Figure()
        
        # Add confidence intervals
        fig.add_trace(go.Scatter(
            x=dates + dates[::-1],
            y=maxs + mins[::-1],
            fill='toself',
            fillcolor='rgba(0,100,80,0.2)',
            line=dict(color='rgba(255,255,255,0)'),
            name='95% Confidence Interval',
            showlegend=True
        ))
        
        fig.add_trace(go.Scatter(
            x=dates + dates[::-1],
            y=p75 + p25[::-1],
            fill='toself',
            fillcolor='rgba(0,100,80,0.4)',
            line=dict(color='rgba(255,255,255,0)'),
            name='50% Confidence Interval',
            showlegend=True
        ))
        
        # Add mean forecast
        fig.add_trace(go.Scatter(
            x=dates,
            y=means,
            mode='lines+markers',
            name='Mean Forecast',
            line=dict(color='red', width=3),
            marker=dict(size=6)
        ))
        
        fig.update_layout(
            title=f'Monte Carlo Forecast - {target_col.upper()}',
            xaxis_title='Date',
            yaxis_title=f'{target_col.upper()}',
            hovermode='x unified',
            showlegend=True
        )
        
        return fig
    
    def create_analysis_chart(self, forecast_result):
        """Create seasonality and peak analysis chart"""
        seasonality = forecast_result.get('seasonality', {})
        peak_info = forecast_result.get('peak_info', {})
        
        fig = make_subplots(
            rows=2, cols=2,
            subplot_titles=('Monthly Seasonality', 'Weekly Seasonality', 
                          'Peak Analysis', 'Overall Stats'),
            specs=[[{"type": "bar"}, {"type": "bar"}],
                   [{"type": "bar"}, {"type": "bar"}]]
        )
        
        # Monthly seasonality
        monthly_pattern = seasonality.get('monthly_pattern', {})
        if monthly_pattern:
            months = list(monthly_pattern.keys())
            values = list(monthly_pattern.values())
            fig.add_trace(go.Bar(x=months, y=values, name='Monthly Avg'), row=1, col=1)
        
        # Weekly seasonality
        weekly_pattern = seasonality.get('weekly_pattern', {})
        if weekly_pattern:
            days = list(weekly_pattern.keys())
            values = list(weekly_pattern.values())
            day_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
            day_labels = [day_names[d] if d < len(day_names) else f'Day {d}' for d in days]
            fig.add_trace(go.Bar(x=day_labels, y=values, name='Weekly Avg'), row=1, col=2)
        
        # Peak analysis
        peak_data = {
            'Peaks': [peak_info.get('peak_count', 0)],
            'Troughs': [peak_info.get('trough_count', 0)],
            'Peak %': [peak_info.get('peak_percentage', 0)]
        }
        for key, value in peak_data.items():
            fig.add_trace(go.Bar(x=[key], y=value, name=key), row=2, col=1)
        
        # Overall stats
        stats_data = {
            'Mean': [seasonality.get('overall_mean', 0)],
            'Std': [seasonality.get('overall_std', 0)],
            'Max': [peak_info.get('max_value', 0)]
        }
        for key, value in stats_data.items():
            fig.add_trace(go.Bar(x=[key], y=value, name=key), row=2, col=2)
        
        fig.update_layout(
            title_text="Seasonality & Peak Analysis",
            showlegend=False,
            height=600
        )
        
        return fig
    
    def create_metrics_table(self, forecast_result, sku_data=None, target_col='qtton'):
        """Create metrics table with forecast accuracy metrics"""
        simulation_stats = forecast_result.get('simulation_stats', {})
        peak_info = forecast_result.get('peak_info', {})
        
        # Calculate forecast accuracy metrics if we have historical data
        accuracy_metrics = {}
        if sku_data is not None and not sku_data.empty and target_col in sku_data.columns:
            try:
                # Get recent actual data for comparison (most recent data)
                # Sort by date to ensure we get the most recent data
                if 'date_short' in sku_data.columns:
                    sku_data_sorted = sku_data.sort_values('date_short', ascending=False)
                    # Get as much recent data as available, but at least match forecast period
                    forecast_period = len(forecast_result['forecasts'])
                    recent_data = sku_data_sorted.head(max(forecast_period, 7))  # Get at least forecast period or 7 days
                else:
                    recent_data = sku_data.tail(max(len(forecast_result['forecasts']), 7))  # Get at least forecast period or 7 days
                
                # Need at least 3 days for basic metrics, or match forecast period if shorter
                min_required = min(3, len(forecast_result['forecasts']))
                if len(recent_data) >= min_required:
                    # Get the number of forecast periods we have
                    forecast_period = len(forecast_result['forecasts'])
                    
                    # Take only the number of historical data points that match our forecast period
                    actual_values = recent_data[target_col].tolist()[:forecast_period]
                    predicted_values = [f['mean'] for f in forecast_result['forecasts']]
                    
                    # Ensure both arrays have the same length
                    min_length = min(len(actual_values), len(predicted_values))
                    actual_values = actual_values[:min_length]
                    predicted_values = predicted_values[:min_length]
                    
                    # Calculate metrics
                    accuracy_metrics = self.calculate_metrics(actual_values, predicted_values)
                    print(f"🔍 Debug: Calculated metrics with {len(actual_values)} data points (forecast period: {forecast_period})")
                    print(f"🔍 Debug: MAE={accuracy_metrics.get('mae', 'N/A')}, MAPE={accuracy_metrics.get('mape', 'N/A')}, RMSE={accuracy_metrics.get('rmse', 'N/A')}")
            except Exception as e:
                print(f"⚠️  Could not calculate accuracy metrics: {e}")
                import traceback
                traceback.print_exc()
        
        data = [
            {'Metric': 'Total Simulations', 'Value': simulation_stats.get('total_simulations', 0)},
            {'Metric': 'Mean Forecast', 'Value': f"{simulation_stats.get('mean_forecast', 0):.2f}"},
            {'Metric': 'Forecast Std', 'Value': f"{simulation_stats.get('std_forecast', 0):.2f}"},
            {'Metric': 'Peak Count', 'Value': peak_info.get('peak_count', 0)},
            {'Metric': 'Peak %', 'Value': f"{peak_info.get('peak_percentage', 0):.1f}%"},
            {'Metric': 'Peak Intensity', 'Value': f"{peak_info.get('peak_intensity', 0):.2f}x"}
        ]
        
        # Add accuracy metrics if available
        if accuracy_metrics and not all(np.isnan([accuracy_metrics.get('mae', np.nan), 
                                                 accuracy_metrics.get('mape', np.nan), 
                                                 accuracy_metrics.get('rmse', np.nan)])):
            data.extend([
                {'Metric': 'MAE (Mean Absolute Error)', 'Value': f"{accuracy_metrics.get('mae', 0):.2f}"},
                {'Metric': 'MAPE (Mean Absolute % Error)', 'Value': f"{accuracy_metrics.get('mape', 0):.1f}%"},
                {'Metric': 'RMSE (Root Mean Square Error)', 'Value': f"{accuracy_metrics.get('rmse', 0):.2f}"},
                {'Metric': 'Data Points Used', 'Value': accuracy_metrics.get('n_points', 0)}
            ])
        else:
            # Provide more specific error message
            if sku_data is None or sku_data.empty:
                error_msg = 'N/A - No historical data available'
            elif target_col not in sku_data.columns:
                error_msg = f'N/A - Column {target_col} not found in data'
            else:
                forecast_period = len(forecast_result['forecasts'])
                min_required = min(3, forecast_period)
                error_msg = f'N/A - Need at least {min_required} data points (have {len(sku_data)} records)'
            
            data.extend([
                {'Metric': 'MAE (Mean Absolute Error)', 'Value': error_msg},
                {'Metric': 'MAPE (Mean Absolute % Error)', 'Value': error_msg},
                {'Metric': 'RMSE (Root Mean Square Error)', 'Value': error_msg}
            ])
        
        try:
            forecasts = forecast_result.get('forecasts', [])

            if forecasts:
                # Handle both dict-based and numeric forecasts
                first_item = forecasts[0]

                if isinstance(first_item, dict) and 'mean' in first_item:
                    forecast_values = [f['mean'] for f in forecasts]
                else:
                    forecast_values = list(forecasts)

                mean_forecast = np.mean(forecast_values)

                data.append({
                    'Metric': '⭐ Mean Forecasted QTTON (daily avg)',
                    'Value': f"{mean_forecast:.2f} tons"
                })
        except Exception as e:
            print(f"⚠️ Could not compute mean forecast QTTON: {e}")
        
        return dash_table.DataTable(
            data=data,
            columns=[{'name': 'Metric', 'id': 'Metric'},
                    {'name': 'Value', 'id': 'Value'}],
            style_cell={'textAlign': 'left'},
            style_header={'backgroundColor': 'rgb(230, 230, 230)', 'fontWeight': 'bold'},
            style_data_conditional=[
                {
                    'if': {'filter_query': '{Metric} contains "Mean Forecasted QTTON"'},
                    'backgroundColor': '#FFF3CD',
                    'fontWeight': 'bold'
                }
            ]
        )

#--------- Helpers for Best Metrics Windowed-----------------
    
    def compute_forecast_metrics(self, forecast_result, sku_data, target_col='qtton'):
    # """
    # Safely compute metrics regardless of forecast format.
    # Supports:
    #   - [{'mean': x}, ...]
    #   - [x, x, x]
    #   - numpy arrays
    # """
        if not forecast_result or 'forecasts' not in forecast_result:
            return None

        forecasts = forecast_result.get('forecasts', [])
        if forecasts is None or len(forecasts) == 0:
            return None

        # ✅ Normalize predictions
        # first_item = forecasts[0]

        # if isinstance(first_item, dict) and 'mean' in first_item:
        #     preds = [f['mean'] for f in forecasts]
        # else:
        #     # assume list/array of numbers
        #     preds = list(forecasts)

        # Case 1: single numeric value
        if isinstance(forecasts, (int, float, np.number)):
            preds = [float(forecasts)]

        # Case 2: numpy array
        elif isinstance(forecasts, np.ndarray):
            preds = forecasts.tolist()

        # Case 3: list
        elif isinstance(forecasts, list):
            if len(forecasts) == 0:
                return None

            first_item = forecasts[0]
            if isinstance(first_item, dict) and 'mean' in first_item:
                preds = [f['mean'] for f in forecasts]
            else:
                preds = list(forecasts)

        else:
            return None

        if sku_data is None or sku_data.empty or target_col not in sku_data.columns:
            return None

        actuals = sku_data[target_col].tail(len(preds)).values

        min_len = min(len(actuals), len(preds))
        if min_len < 3:
            return {
                'mae': np.nan,
                'rmse': np.nan,
                'mape': np.nan,
                'n_points': min_len
            }


        actuals = np.array(actuals[:min_len])
        preds = np.array(preds[:min_len])

        mae = mean_absolute_error(actuals, preds)
        rmse = np.sqrt(mean_squared_error(actuals, preds))
        mape = np.mean(np.abs((actuals - preds) / (actuals + 1e-8))) * 100

        return {
            'mae': mae,
            'rmse': rmse,
            'mape': mape,
            'n_points': min_len
        }


    def select_best_model(self, model_results, historical_data, target_col='qtton'):
        scored = []

        for model_name, result in model_results.items():
            metrics = self.compute_forecast_metrics(
                result.get('forecasts', []),
                historical_data,
                target_col
            )
            if metrics:
                scored.append((model_name, result, metrics))

        if not scored:
            return None, None, None

        # Business rule: RMSE wins
        best = min(scored, key=lambda x: x[2]['rmse'])
        return best  # (model_name, result, metrics)
    
    def render_best_model_metrics_table(self, model_name, metrics, forecasts):
        mean_forecast = np.mean([f['mean'] for f in forecasts])
        total_forecast = np.sum([f['mean'] for f in forecasts])

        table_data = [
            {'Metric': 'Best Model', 'Value': model_name},
            {'Metric': 'MAE', 'Value': f"{metrics['mae']:.2f}"},
            {'Metric': 'MAPE', 'Value': f"{metrics['mape']:.1f}%"},
            {'Metric': 'RMSE', 'Value': f"{metrics['rmse']:.2f}"},
            {'Metric': 'Data Points Used', 'Value': metrics['n_points']},
            {'Metric': 'Mean Forecast (daily)', 'Value': f"{mean_forecast:.2f}"},
            {'Metric': 'Total Forecast', 'Value': f"{total_forecast:.2f}"}
        ]

        return dash_table.DataTable(
            data=table_data,
            columns=[{'name': 'Metric', 'id': 'Metric'},
                    {'name': 'Value', 'id': 'Value'}],
            style_cell={'textAlign': 'left'},
            style_header={'fontWeight': 'bold'}
        )

#-----------------------------------------------------------

    def create_overall_metrics_display(self, results):
        """Create overall metrics display"""
        overall = results['overall_metrics']
        
        cards = [
            dbc.Card([
                dbc.CardBody([
                    html.H4(f"{overall['mae']:.2f}", className="text-primary"),
                    html.P("MAE", className="card-text")
                ])
            ], className="mb-2"),
            
            dbc.Card([
                dbc.CardBody([
                    html.H4(f"{overall['rmse']:.2f}", className="text-warning"),
                    html.P("RMSE", className="card-text")
                ])
            ], className="mb-2"),
            dbc.Card([
                dbc.CardBody([
                    html.H4(f"{overall['total_skus']}", className="text-info"),
                    html.P("SKUs Analyzed", className="card-text")
                ])
            ], className="mb-2")
        ]
        
        return html.Div(cards)
    
    def create_report_summary_display(self, report):
        """Create summary display for comprehensive report"""
        metadata = report['metadata']
        summary = report['summary']
        
        # Create summary cards
        level_name = 'Families' if metadata['level'] == 'family' else 'SKUs'
        cards = [
            dbc.Card([
                dbc.CardBody([
                    html.H4(f"{metadata['processed_items']}", className="text-primary"),
                    html.P(f"{level_name} Processed", className="card-text mb-0")
                ])
            ], className="text-center mb-2"),
            dbc.Card([
                dbc.CardBody([
                    html.H4(f"{metadata['failed_items']}", className="text-warning"),
                    html.P(f"{level_name} Failed", className="card-text mb-0")
                ])
            ], className="text-center mb-2"),
            dbc.Card([
                dbc.CardBody([
                    html.H4(f"{metadata['target_col'].upper()}", className="text-info"),
                    html.P("Target Metric", className="card-text mb-0")
                ])
            ], className="text-center mb-2"),
            dbc.Card([
                dbc.CardBody([
                    html.H4(f"{metadata['level'].upper()}", className="text-success"),
                    html.P("Analysis Level", className="card-text mb-0")
                ])
            ], className="text-center mb-2")
        ]
        
        # Add time horizon summaries
        horizon_cards = []
        time_horizons = metadata['time_horizons']
        use_all_models = metadata.get('use_all_models', False)
        
        for horizon_name in time_horizons.keys():
            if horizon_name in summary:
                horizon_summary = summary[horizon_name]
                item_name = 'families' if metadata['level'] == 'family' else 'SKUs'
                
                # Create model comparison info if using all models
                model_info = ""
                if use_all_models and 'model_comparison' in horizon_summary:
                    model_count = len(horizon_summary.get('model_comparison', {}))
                    model_info = f" ({model_count} models)"
                
                horizon_cards.append(
                    dbc.Card([
                        dbc.CardHeader(f"📊 {horizon_name.replace('_', ' ').title()}{model_info}"),
                        dbc.CardBody([
                            html.P(f"Total {item_name}: {horizon_summary['total_skus']}", className="mb-1"),
                            html.P(f"Grand Total: {horizon_summary['grand_total']:.2f}", className="mb-1"),
                            html.P(f"Avg per {metadata['level']}: {horizon_summary['average_per_sku']:.2f}", className="mb-1"),
                            html.P(f"Daily Avg: {horizon_summary['average_daily_per_sku']:.2f}", className="mb-0")
                        ])
                    ], className="mb-2")
                )
        
        return html.Div([
            html.H5("📊 Report Summary", className="mb-3"),
            html.Div(cards, className="mb-4"),
            html.H6("📈 Time Horizon Summaries", className="mb-3"),
            html.Div(horizon_cards)
        ])
    
    def create_report_table_display(self, report):
        """Create detailed table display for comprehensive report"""
        predictions = report['predictions']
        metadata = report['metadata']
        
        if not predictions:
            return "No predictions data available"
        
        # Create table data
        table_data = []
        time_horizons = metadata['time_horizons']
        level_name = metadata['level'].upper()
        use_all_models = metadata.get('use_all_models', False)
        
        for item_pred in predictions:
            item = item_pred['item']
            row = {
                level_name: item,
                'Data Points': item_pred['data_points']
            }
            
            # Add data for each time horizon
            for horizon_name in time_horizons.keys():
                horizon_pred = item_pred['predictions'].get(horizon_name)
                if horizon_pred and horizon_pred['summary']:
                    summary = horizon_pred['summary']
                    
                    if use_all_models and len(summary) > 1:
                        # Multiple models - show best performing model based on minimum MAE
                        best_model = None
                        best_mae = float('inf')
                        
                        # Get metrics if available
                        print(horizon_pred)
                        metrics = horizon_pred.get('metrics', {})
                        print("---------------------------------------------")
                        print(metrics)
                        
                        for model_name, model_summary in summary.items():
                            # Get MAE for this model
                            model_mae = None
                            if metrics and model_name in metrics and metrics[model_name]:
                                model_mae = metrics[model_name]['mae']
                            
                            # Select model with minimum MAE (if MAE is available)
                            if model_mae is not None and model_mae < best_mae:
                                best_mae = model_mae
                                best_model = model_name
                        
                        # Fallback: if no metrics available, use first model
                        if best_model is None and len(summary) > 0:
                            best_model = list(summary.keys())[0]
                        
                        if best_model:
                            model_summary = summary[best_model]
                            # Get metrics for best model
                            best_model_metrics = metrics.get(best_model, {}) if metrics else {}
                            row.update({
                                f'{horizon_name}_Total': f"{model_summary['total_predicted']:.2f}",
                                f'{horizon_name}_Daily_Avg': f"{model_summary['daily_average']:.2f}",
                                f'{horizon_name}_Trend': model_summary['trend'],
                                f'{horizon_name}_Best_Model': best_model,
                                f'{horizon_name}_Best_MAE': f"{best_model_metrics.get('mae', 'N/A'):.4f}" if best_model_metrics.get('mae') else 'N/A'
                            })
                        else:
                            row.update({
                                f'{horizon_name}_Total': 'N/A',
                                f'{horizon_name}_Daily_Avg': 'N/A',
                                f'{horizon_name}_Trend': 'N/A',
                                f'{horizon_name}_Best_Model': 'N/A',
                                f'{horizon_name}_Best_MAE': 'N/A'
                            })
                    else:
                        # Single model or Monte Carlo only
                        first_model = list(summary.keys())[0] if summary else None
                        if first_model:
                            model_summary = summary[first_model]
                            row.update({
                                f'{horizon_name}_Total': f"{model_summary['total_predicted']:.2f}",
                                f'{horizon_name}_Daily_Avg': f"{model_summary['daily_average']:.2f}",
                                f'{horizon_name}_Trend': model_summary['trend']
                            })
                        else:
                            row.update({
                                f'{horizon_name}_Total': 'N/A',
                                f'{horizon_name}_Daily_Avg': 'N/A',
                                f'{horizon_name}_Trend': 'N/A'
                            })
                else:
                    row.update({
                        f'{horizon_name}_Total': 'N/A',
                        f'{horizon_name}_Daily_Avg': 'N/A',
                        f'{horizon_name}_Trend': 'N/A'
                    })
                    if use_all_models:
                        row[f'{horizon_name}_Best_Model'] = 'N/A'
            
            table_data.append(row)
        
        # Sort by the first available horizon total (descending)
        sort_key = None
        for horizon_name in time_horizons.keys():
            if f'{horizon_name}_Total' in table_data[0]:
                sort_key = f'{horizon_name}_Total'
                break
        
        if sort_key:
            table_data.sort(key=lambda x: float(x[sort_key]) if x[sort_key] != 'N/A' else 0, reverse=True)
        
        # Create columns dynamically based on time horizons
        columns = [
            {'name': level_name, 'id': level_name, 'type': 'text'},
            {'name': 'Data Points', 'id': 'Data Points', 'type': 'numeric'}
        ]
        
        for horizon_name in time_horizons.keys():
            columns.extend([
                {'name': f'{horizon_name.replace("_", " ").title()} Total', 'id': f'{horizon_name}_Total', 'type': 'numeric', 'format': {'specifier': '.2f'}},
                {'name': f'{horizon_name.replace("_", " ").title()} Avg', 'id': f'{horizon_name}_Daily_Avg', 'type': 'numeric', 'format': {'specifier': '.2f'}},
                {'name': f'{horizon_name.replace("_", " ").title()} Trend', 'id': f'{horizon_name}_Trend', 'type': 'text'}
            ])
            
            # Add best model column if using all models
            if use_all_models:
                columns.extend([
                    {'name': f'{horizon_name.replace("_", " ").title()} Best Model', 'id': f'{horizon_name}_Best_Model', 'type': 'text'},
                    {'name': f'{horizon_name.replace("_", " ").title()} Best MAE', 'id': f'{horizon_name}_Best_MAE', 'type': 'numeric', 'format': {'specifier': '.4f'}}
                ])
            
        return html.Div([
            html.H6(f"📋 Detailed {level_name} Predictions", className="mb-3"),
            dash_table.DataTable(
                data=table_data,
                columns=columns,
                style_cell={
                    'textAlign': 'left', 
                    'padding': '8px',
                    'fontSize': '12px',
                    'fontFamily': 'Arial, sans-serif'
                },
                style_header={
                    'backgroundColor': 'rgb(52, 58, 64)', 
                    'color': 'white',
                    'fontWeight': 'bold',
                    'textAlign': 'center'
                },
                style_data_conditional=[
                    {
                        'if': {'row_index': 'odd'},
                        'backgroundColor': 'rgb(248, 249, 250)'
                    },
                    {
                        'if': {'column_id': level_name},
                        'backgroundColor': 'rgb(220, 248, 198)',
                        'fontWeight': 'bold'
                    }
                ],
                page_size=20,
                sort_action="native",
                filter_action="native"
            )
        ]),table_data
    
    def create_raw_material_summary(self, raw_material_result):
        """Create SKU-specific raw material summary display"""
        summary = raw_material_result['summary']
        sku = raw_material_result.get('sku', 'Unknown')
        sku_formula = raw_material_result.get('sku_formula', 'Unknown')
        
        # Calculate total raw material requirement
        total_requirement = sum(data['total_amount'] for data in summary.values())
        
        cards = [
            dbc.Card([
                dbc.CardBody([
                    html.H6(f"🎯 SKU: {sku}", className="text-primary mb-1"),
                    html.H6(f"📋 Formula: {sku_formula}", className="text-info mb-1"),
                    html.H5(f"{raw_material_result['total_raw_materials']}", className="text-primary"),
                    html.P("Raw Materials", className="card-text mb-0")
                ])
            ], className="text-center mb-2"),
            dbc.Card([
                dbc.CardBody([
                    html.H5(f"{raw_material_result['forecast_period']}", className="text-success"),
                    html.P("Forecast Days", className="card-text mb-0")
                ])
            ], className="text-center mb-2"),
            dbc.Card([
                dbc.CardBody([
                    html.H5(f"{total_requirement:.2f}", className="text-warning"),
                    html.P("Total Tons Required", className="card-text mb-0")
                ])
            ], className="text-center mb-2")
        ]
        
        return html.Div(cards)
    
    def create_raw_material_table(self, raw_material_result):
        """Create SKU-specific raw material detailed table"""
        raw_material_forecast = raw_material_result['raw_material_forecast']
        
        if not raw_material_forecast:
            return "No raw material data available"
        
        # Get SKU information
        sku = raw_material_result.get('sku', 'Unknown')
        sku_formula = raw_material_result.get('sku_formula', 'Unknown')
        forecast_period = raw_material_result.get('forecast_period', 0)
        
        # Create SKU-specific summary data for table
        summary_data = []
        for raw_mat, data in raw_material_result['summary'].items():
            # Calculate daily average requirement
            daily_avg = data['total_amount'] / max(forecast_period, 1)
            
            summary_data.append({
                'Raw Material': data['raw_name'],
                'Code': raw_mat,
                'Formula %': f"{data['ton_percentage']:.1f}%",
                'Total Required (tons)': f"{data['total_amount']:.2f}",
                'Daily Avg (tons)': f"{daily_avg:.2f}",
                'SKU': sku
            })
        
        # Sort by percentage (highest first)
        summary_data.sort(key=lambda x: float(x['Formula %'].replace('%', '')), reverse=True)
        
        return html.Div([
            html.H6(f"📋 Formula: {sku_formula}", className="mb-2"),
            html.P(f"🎯 SKU: {sku} | Period: {forecast_period} days", className="text-muted mb-3"),
            dash_table.DataTable(
                data=summary_data,
                columns=[
                    {'name': 'Raw Material', 'id': 'Raw Material', 'type': 'text'},
                    {'name': 'Code', 'id': 'Code', 'type': 'text'},
                    {'name': 'Formula %', 'id': 'Formula %', 'type': 'text'},
                    {'name': 'Total Required (tons)', 'id': 'Total Required (tons)', 'type': 'numeric', 'format': {'specifier': '.2f'}},
                    {'name': 'Daily Avg (tons)', 'id': 'Daily Avg (tons)', 'type': 'numeric', 'format': {'specifier': '.2f'}},
                    {'name': 'SKU', 'id': 'SKU', 'type': 'text'}
                ],
                style_cell={
                    'textAlign': 'left', 
                    'padding': '8px',
                    'fontSize': '14px',
                    'fontFamily': 'Arial, sans-serif'
                },
                style_header={
                    'backgroundColor': 'rgb(52, 58, 64)', 
                    'color': 'white',
                    'fontWeight': 'bold',
                    'textAlign': 'center'
                },
                style_data_conditional=[
                    {
                        'if': {'row_index': 'odd'},
                        'backgroundColor': 'rgb(248, 249, 250)'
                    },
                    {
                        'if': {'column_id': 'Formula %'},
                        'backgroundColor': 'rgb(220, 248, 198)',
                        'fontWeight': 'bold'
                    },
                    {
                        'if': {'column_id': 'Total Required (tons)'},
                        'backgroundColor': 'rgb(173, 216, 230)'
                    },
                    {
                        'if': {'column_id': 'Daily Avg (tons)'},
                        'backgroundColor': 'rgb(255, 218, 185)'
                    }
                ],
                page_size=15,
                sort_action="native",
                filter_action="native"
            )
        ])
    
    def run_app(self):
        """Run the complete application"""
        print("🚀 STARTING MULTI-MODEL FORECASTING APPLICATION")
        print("=" * 60)
        print("🤖 7 Forecasting Models: Monte Carlo, ARIMA, Linear Regression, Ridge, Lasso, Random Forest, XGBoost")
        print("📊 Focused on MAE, MAPE, and RMSE metrics")
        print("🗑️  Fresh database creation (no caching)")
        print("=" * 60)
        
        # Load CSV data
        if os.path.isdir(DATA_DIR) and any(f.endswith(".csv") for f in os.listdir(DATA_DIR)):
            print(f"\n📊 Found CSV files in {DATA_DIR} - Loading data...")
            if self.load_csv_data():
                print("✅ CSV data loaded successfully!")
            else:
                print("⚠️  CSV loading failed - continuing with existing data")
        else:
            print(f"\n⚠️  No CSV files found in {DATA_DIR}")
        
        # Setup dashboard
        self.setup_dashboard()
        
        # Start the app
        print("\n🚀 Launching Multi-Model Dashboard...")
        print("📊 Dashboard available at: http://localhost:8051")
        print("🎯 FEATURES:")
        print("  🤖 7 Forecasting Models: Monte Carlo, ARIMA, Linear Regression, Ridge, Lasso, Random Forest, XGBoost")
        print("  📈 Model comparison with side-by-side visualization")
        print("  📊 MAE, MAPE, RMSE metrics for all models")
        print("  🎯 Overall metrics for all SKUs")
        print("  🏭 Raw material forecasting")
        print("  📊 Comprehensive predictions report (SKU & Family level)")
        print("  📅 Forward & Historical analysis (1 day, 1 week, 2 weeks + last 10 instances)")
        print("  📁 CSV export functionality with model comparison")
        print("Press Ctrl+C to stop")
        
        # Run the app
        self.app.run(host='0.0.0.0', port=8051, debug=False)

def main():
    """Main function"""
    app = MonteCarloForecaster(fresh_start=True)
    app.run_app()

if __name__ == "__main__":
    main()

import os
import time
import pandas as pd
from functools import partial
import os
from datetime import datetime
import matplotlib.pyplot as plt
import numpy as np
from scipy import stats
from IPython.display import clear_output, display
import matplotlib.dates as mdates

def AFIdffx(path):
    columns_to_load = ['cow','grp', 'dim', 'daily_yield' ]
    # Get all parquet files
    files = [f for f in os.listdir(path) if f.endswith('.csv')]
    
    # Read each file and add filename column
    dfs = []
    for file in files:
        file_path = os.path.join(path, file)
        temp_df = pd.read_csv(file_path, usecols=columns_to_load)
        temp_df['file_name'] = file  # Add filename column
        dfs.append(temp_df)
    # Concatenate all dataframes
    df2 = pd.concat(dfs, ignore_index=True)
    # also add the date
    df2['date'] = df2['file_name'].str.extract(r'(\d{2}-\d{2}-\d{4})', expand=True)
    df2['date'] = pd.to_datetime(df2['date'], format='%m-%d-%Y')
    df2= df2.sort_values(by='date', ascending= False)
    df2.drop('file_name', axis=1, inplace=True)
    df2 = df2.rename(columns={'grp': 'group'})
    df2 = df2.rename(columns={'farm_animal_id': 'animal_id'})
    
    # 2) Parse date → day and ensure numeric metrics
    df2['date'] = pd.to_datetime(df2['date'], errors='coerce').dt.floor('D')
    for c in ['dim', 'daily_yield']:
        df2[c] = pd.to_numeric(df2[c], errors='coerce')
   
    agg = (df2.dropna(subset=['date', 'group'])
         .groupby(['date', 'group'], as_index=False)
         .agg(
             # calculate the the average DIM nad Yield.
             # calculae std dev
             # se = standard error
             avg_dim=('dim', 'mean'),
             avg_daily_yield=('daily_yield', 'mean'),
             #just std in the next line already makes the bessel correction
             avg_daily_yield_std=('daily_yield',  partial(pd.Series.std, ddof=1)),
             n_animals=('cow', 'nunique')   # optional but handy
             
         ))
  
    agg['se'] = (agg['avg_daily_yield_std'])/(np.sqrt(agg['n_animals']))
    # 4) Add month name (from the aggregated day) and sort
    agg['month_name'] = agg['date'].dt.month_name()
    agg = agg.sort_values(['group', 'date'])
    
    # 5) (optional) Reorder columns
    agg = agg[['date', 'month_name', 'group', 'avg_dim', 'avg_daily_yield', 'avg_daily_yield_std', 'n_animals','se']]
    agg = agg.round(decimals=2)
    
    return  agg

def ConfInts(dfi):
    alpha = 0.20
    dfi['t_value'] = stats.t.ppf(1 - alpha/2.0, dfi['n_animals'] - 1)
    #margin of error
    moe= dfi['t_value']*dfi['se']
    dfi['ci_lower']= dfi['avg_daily_yield']-moe
    dfi['ci_upper']= dfi['avg_daily_yield']+moe
    return dfi
    
"""
def LineGrph_CI(to_plot):
    ADdf= to_plot
    ADdf = ADdf[ADdf['group'].isin([1, 2, 3, 4, 5])]

    # Plot: one figure, each group a line + shaded CI
    fig, ax = plt.subplots(figsize=(12, 6))
    #ADdf = ADdf[(ADdf.Group==1)]
    for g, sub in ADdf.sort_values(['date']).groupby('group'):
        ax.plot(sub['date'], sub['avg_daily_yield'], marker='o', linewidth=1.5, label=str(g))
        # Shade CI (skip rows with n<2 which give NaN CI)
        ok = sub['n_animals'] >= 2
        ax.fill_between(sub.loc[ok, 'date'],
                        sub.loc[ok, 'ci_lower'],
                        sub.loc[ok, 'ci_upper'],
                        alpha=0.4)
   
    ax.set_title('Daily Mean Yield with 80% t-Confidence Intervals by Group')
    ax.set_xlabel('Date'); ax.set_ylabel('Mean Daily Yield')
    ax.legend(title='Group'); ax.grid(alpha=0.3)
    # Format the x-axis to show dates properly
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m/%d/%Y'))  # or '%Y-%m-%d' for YYYY-MM-DD
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())  # Automatically space the dates
    # Rotate date labels for better readability
    plt.xticks(rotation=45, ha='right')
    fig.autofmt_xdate(); plt.tight_layout() 
    plt.show()
    return fig

"""
def process_and_plot(folder_path):
    """Read all files and create plot"""
    # Call your existing function to get the dataframe

    df = AFIdffx(folder_to_watch)
    
    # Or if you have another function to read and process data:
   
    df_ci=ConfInts(df)
    df_ci.to_csv('data.csv', index=False)
    #fig = LineGrph_CI(df_ci)
    
    #print(f"Files processed: {len(df)}")
    #print(df.head())
    # Add your plotting here

def monitor_folder_polling(folder_path, interval=30):
    #intervaL in seconds
    """Monitors a folder and updates plot when new files arrive."""
    initial_files = set(os.listdir(folder_path))
    print(f"Initial files: {len(initial_files)}")
    
    # Initial plot
    clear_output(wait=True)
    process_and_plot(folder_path)
      

    while True:
        time.sleep(interval)
        current_files = set(os.listdir(folder_path))
        new_files = current_files - initial_files
        
        if new_files:
            print(f"New files detected: {new_files}")
            initial_files = current_files
            
            # Clear and redraw
            clear_output(wait=True)
            process_and_plot(folder_path)
        
        #print(f"Check {i+1}/{max_checks}...")     

# Run it
# this folder is a trial folder where I upload the files manually.
folder_to_watch = "/data"
monitor_folder_polling(folder_to_watch, interval=30)



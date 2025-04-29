# monsoon-onset
Operational version of the data-driven monsoon onset


## Fixes necessary for the IMERG pipeline

### Details 
imerg_daily.sh is the script that needs to be triggered on a daily basis to produce the most up-to-date IMERG graphs.
    - Currently pulling data using a wget with my (Tyler Yang's) credentials - To get this to run on Midway cluster, I will either probably have to set up the cookies file again or use another person's credentials
    - Does eventually need to be changed if we are changing to the real time IMD/IMERG set
    - This pulls in the ***IMERG Late run Data *** (appears 14 hours after day end)

imerg_all.sh is the script for downloading multiple day files at once if you need to
    - Should be done during the first time you set up the pipeline - make sure you change the for loop parameters to get this to work
    - You can modify the for loop parameters to download a certain number of days' worth of IMERG data.

Indian Monsoon Script is the data processing script to produce graphs and time series.

### Path resolution necessary to get to work



In indian_monsoon_script.py -> Need to resolve the following path dependencies

- Lines 231, 245 -> This path should be the folder of where the IMERG_daily data is stored -> its imperitive that at the very least this has the data for all of 2025 until now
- Line 259 -> This path needs to be where you want the daily IMERG files to be.
- Lines 198, 283 -> this should be the path to the "grid_2x2_dissem" csv file
- Line 262 -> MWmean.npy file that exists in this directory
- Lines 223, 294 -> This should be the same from line 259.

In imerg_daily.sh and imerg_all.sh

- path to indian_monsoon_script.py needs to be changed
- Cookies credential file for wget commands need to be changed.
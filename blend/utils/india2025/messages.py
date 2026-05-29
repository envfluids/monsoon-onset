import pandas as pd
from datetime import timedelta
import math
import logging

logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s - %(levelname)s - %(name)s - "
        "%(pathname)s:%(lineno)d - %(message)s"
    ),
)


def round_5(x):
    return round(x * 20) / 20

def round_down(x):
    return math.floor(x * 20) / 20


def generate_messages(base, out_path, onset_times):
    # root = "/mnt/share/DIL/weather/"  #Change these to match where things will be on the cluster 
    # path = "Code/py/"
    support_path = base / "blend" / "data" / "support"
    xlsx_path = support_path / "master_translation_sheet.xlsx"
    exclude_path = support_path / "exclude_cells.csv"

    out_path = out_path / "messages"
    out_path.mkdir(exist_ok=True)

    #############################
    # INPUT DATA LOADING & FILTERS
    #############################
    # Read forecast onset times and exclusion cells
    # onset_times = pd.read_csv(f'{root}{path}blend_output_summary.csv')

    df_exclude = pd.read_csv(exclude_path)

    # Exclude rows flagged "exclude" from onset file
    if 'flag' in onset_times.columns:
        onset_times = onset_times[onset_times['flag'] != 'exclude']
    # Exclude cells listed in exclude_cells file
    exclude_set = set(
        df_exclude.loc[df_exclude['flag']=='exclude', ['lon','lat']]
        .itertuples(index=False, name=None)
    )
    onset_times = onset_times[~onset_times.set_index(['lon','lat']).index.isin(exclude_set)]

    ##################################
    # LOAD TRANSLATION & TEMPLATE SHEETS
    ##################################

    #Sheets containing translations of words or phrases
    months_df  = pd.read_excel(xlsx_path, sheet_name='months', engine='openpyxl', header=0)
    qual_df    = pd.read_excel(xlsx_path, sheet_name='qualitative', engine='openpyxl', header=0)
    grid_state = pd.read_excel(xlsx_path, sheet_name='grid_box', engine='openpyxl', header=0)

    grid_state.rename(columns={'campaign':'campaign_name'}, inplace=True)
    #Sheets containing message templates
    #LS1: three bins (LS = "Late Season")
    #LS3: two bins, emphasize first (when "week1" or "week1 + week2" is selected
    #ES1: two bins, emphasize second (when "later" or "week4 + later" is selected
    LS1_raw   = pd.read_excel(xlsx_path, sheet_name='LS1', engine='openpyxl', header=None)
    LS3_raw   = pd.read_excel(xlsx_path, sheet_name='LS3', engine='openpyxl', header=None)
    ES1_raw   = pd.read_excel(xlsx_path, sheet_name='ES1', engine='openpyxl', header=None)

    ES2_raw     = pd.read_excel(xlsx_path, sheet_name='ES2', engine='openpyxl', header=None)
    LS2_raw     = pd.read_excel(xlsx_path, sheet_name='LS2', engine='openpyxl', header=None)
    LS4_raw     = pd.read_excel(xlsx_path, sheet_name='LS4', engine='openpyxl', header=None)

    LS1_ivrs  = pd.read_excel(xlsx_path, sheet_name='LS1_IVRS', engine='openpyxl', header=None)
    LS3_ivrs  = pd.read_excel(xlsx_path, sheet_name='LS3_IVRS', engine='openpyxl', header=None)
    ES1_ivrs  = pd.read_excel(xlsx_path, sheet_name='ES1_IVRS', engine='openpyxl', header=None)

    # Supported language columns
    #These need to match the languages in master_translation_sheet.xlsx"
    languages = ['Eng','Hin', 'Kan', 'Mar', 'Odi','Pun','Ben','Tel', 'Odi_ivrs', 'Eng_ivrs']

    # Map months_df rows to month numbers (May=5 … August=8)
    month_nums = [5, 6, 7, 8]
    months_df['month_num'] = month_nums[:len(months_df)]

    ###########################################
    # THRESHOLDS & QUALITATIVE MAPPING SETUP
    ###########################################
    high_conf_1     = 0.65  # threshold to return a single week instead of two weeks
    high_conf_later = 0.65  # threshold to return "later" instead of "week 4 + later" 

    short_msg_cutoff = .51 #cutoff to use "short" messages instead of long

    # Qualitative bins: (percent cutoff, qual_df row index)
    qual_bins = [(14,0), (34,1), (65,2), (85,3), (100,4)]

    def pick_template(first, second, ivrs, use_set2=False):
        if ivrs:
            if use_set2:
                if first=='week1' or second=='week1':
                    return LS3_ivrs
                if first=='later' or second=='later':
                    return ES1_ivrs
                return LS1_ivrs
            else:
                if first=='week1' or second=='week1':
                    return LS3_ivrs
                if first=='later' or second=='later':
                    return ES1_ivrs
                return LS1_ivrs
        else:
            if use_set2:
                if first=='week1' or second=='week1':
                    return LS4_raw
                if first=='later' or second=='later':
                    return ES2_raw
                #return LS2_raw
                return LS1_raw
            else:
                if first=='week1' or second=='week1':
                    return LS3_raw
                if first=='later' or second=='later':
                    return ES1_raw
                return LS1_raw

    def pick_template_name(first, second, ivrs, use_set2=False):
        base = ""
        if ivrs:
            base = "(IVR)"
            use_set2 = False
        if use_set2:
            if first=='week1' or second=='week1':
                return f"Late Season 4 {base}".strip()
            if first=='later' or second=='later':
                return f"Early Season 2 {base}".strip()
            #return f"Late Season 2 {base}".strip()
            return f"Late Season 1 {base}".strip()
        else:
            if first=='week1' or second=='week1':
                return f"Late Season 3 {base}".strip()
            if first=='later' or second=='later':
                return f"Early Season 1 {base}".strip()
            return f"Late Season 1 {base}".strip()

    def get_qualifier(pct, col):
        for threshold, idx in qual_bins:
            if pct <= threshold:
                return qual_df.iloc[idx][col]
        return qual_df.iloc[-1][col]

    #########################
    # MESSAGE BUILDING LOGIC
    #########################
    four_week_cols  = ['week1','week2','week3','week4']
    week_pairs      = [('week1','week2'), ('week2','week3'), ('week3','week4')]
    week_pair_later = [('week4','later')]

    messages = []
    intermediate = [] # for intermediate csv -- show probabilities we're sending but without all the words

    for idx, row in onset_times.iterrows():
        lon, lat = row['lon'], row['lat']
        forecast_date = pd.to_datetime(row['time'])
        flag_val = row['flag'] if 'flag' in onset_times.columns else ''

        # Initialize probability buckets
        first_week = second_week = None
        date1 = date2 = None
        percent_before = percent_mid = percent_after = 0

        # Case 1: a specific week (within the first 4) has high or very high confidence
        for col in four_week_cols:
            if row[col] >= high_conf_1:
                idx = int(col[-1])
                first_week = col
                date1 = forecast_date + timedelta(days=7*(idx-1))
                date2 = date1 + timedelta(days=7)
                percent_mid    = row[col]
                if idx == 1:
                    percent_before = None
                else: 
                    percent_before = sum(row[f'week{i}'] for i in range(1, idx))
                percent_after  = sum(row[f'week{i}'] for i in range(idx+1,5)) + row['later']
                break

        # Case 2: the monsoon coming after the 4 weeks is highly likely
        if first_week is None and row['later'] >= high_conf_later:
            first_week = 'later'
            date1 = forecast_date + timedelta(days=28)
            date2 = None
            percent_mid    = row['later']
            percent_before = sum(row[f'week{i}'] for i in range(1,5))
            percent_after  = None

        # Case 3: For cases where any one week could not meet the threshold, cycle through adjacent week pairs to find the one with highest likelihood 
        # Note: No longer requiring this probability to exceed medium threshold
        if first_week is None:
            best = 0
            for w1, w2 in week_pairs + week_pair_later:
                s = row[w1] + row[w2]
                if s > best:
                    best = s
                    first_week, second_week = w1, w2
                    if w1=='week4' and w2=='later':
                        date1 = forecast_date + timedelta(days=21)
                        date2 = None
                        percent_mid    = row['week4'] + row['later']
                        percent_before = sum(row[f'week{i}'] for i in range(1,4))
                        percent_after  = None
                    else:
                        i1, i2 = int(w1[-1]), int(w2[-1])
                        date1 = forecast_date + timedelta(days=7*(i1-1))
                        date2 = forecast_date + timedelta(days=7*i2)
                        percent_mid    = row[w1] + row[w2]
                        if i1 == 1:
                            percent_before = None
                        else: 
                            percent_before = sum(row[f'week{i}'] for i in range(1, i1))
                        percent_after  = sum(row[f'week{i}'] for i in range(i2+1,5)) + row['later']

        # Skip if no period identified
        if first_week is None:
            continue

    ####################
    # ADJUSTING PERCENTS SUCH THAT THEY ROUND TO NEAREST FIVE PERCENT, AREN'T 0 OR 100, AND SUM TO 100
    ####################
        if first_week is not None:
            rounded_before = rounded_mid = rounded_after = None
            # Assigning probabilities when the chosen period includes later
            if percent_after is None: 
                if percent_mid > .95:
                    rounded_mid = .95
                    rounded_before = .05
                else:
                    rounded_mid = round_5(percent_mid)
                    rounded_before = 1 - rounded_mid
            # Assigning probabilities when the chosen period starts with week1
            if percent_before is None: 
                if percent_mid > .95:
                    rounded_mid = .95
                    rounded_after = .05
                else:
                    rounded_mid = round_5(percent_mid)
                    rounded_after = 1 - rounded_mid

            tolerance = 1e-7

            # Assigning rounded values to test rounded sum.
            if percent_before is not None and percent_after is not None:
                percent = [percent_before, percent_mid, percent_after]
                perc_df = pd.DataFrame({'percent': percent})
                perc_df['rounded'] = None
                perc_df['error'] = None
                for p_idx, p_row in perc_df.iterrows():
                    if p_row['percent'] > 0.05:
                        perc_df.loc[p_idx, 'rounded'] = round_down(p_row['percent'])
                        perc_df.loc[p_idx, 'error'] = p_row['percent'] - perc_df.loc[p_idx, 'rounded']
                    else:
                        perc_df.loc[p_idx, 'rounded'] = 0.05
                        perc_df.loc[p_idx, 'error'] = p_row['percent']
                # Checking total and adjusting while the total is not 1
                perc_df['rounded'] = pd.to_numeric(perc_df['rounded'], errors = 'coerce')
                perc_df['error'] = pd.to_numeric(perc_df['error'], errors = 'coerce')

                while abs(perc_df['rounded'].sum()) >= tolerance:
                    
                    # Subtracting from the instance with the lowest error (minimizing the increase in the error) when the sum is over 1.
                    # This should be quite rare.
                    if perc_df['rounded'].sum() > 1: 
                        sorted_df = perc_df.sort_values(by='error').reset_index()
                        subtracted = False
                        if sorted_df.loc[0, 'rounded'] > 0.05:
                            perc_df.loc[sorted_df.loc[0, 'index'], 'rounded'] -= 0.05
                            perc_df.loc[sorted_df.loc[0, 'index'], 'error'] += 0.05
                            subtracted = True
                        elif sorted_df.loc[1, 'rounded'] > 0.05:
                            perc_df.loc[sorted_df.loc[1, 'index'], 'rounded'] -= 0.05
                            perc_df.loc[sorted_df.loc[1, 'index'], 'error'] += 0.05
                            subtracted = True
                        if not subtracted:
                            perc_df.loc[sorted_df.loc[2, 'index'], 'rounded'] -= 0.05
                            perc_df.loc[sorted_df.loc[2, 'index'], 'error'] += 0.05
                    # Adding to the bin with the highest error (minimizing the error post add) when the sum is below 1.
                    elif perc_df['rounded'].sum() < 1:
                        sorted_df = perc_df.sort_values(by='error', ascending=False).reset_index() 
                        added = False
                        if sorted_df.loc[0, 'rounded'] > 0.05:
                            perc_df.loc[sorted_df.loc[0, 'index'], 'rounded'] += 0.05
                            perc_df.loc[sorted_df.loc[0, 'index'], 'error'] -= 0.05
                            added = True
                        elif sorted_df.loc[1, 'rounded'] > 0.05:
                            perc_df.loc[sorted_df.loc[1, 'index'], 'rounded'] += 0.05
                            perc_df.loc[sorted_df.loc[1, 'index'], 'error'] -= 0.05
                            added = True
                        if not added:
                            perc_df.loc[sorted_df.loc[2, 'index'], 'rounded'] += 0.05
                            perc_df.loc[sorted_df.loc[2, 'index'], 'error'] -= 0.05
                    perc_df['rounded'] = pd.to_numeric(perc_df['rounded'], errors = 'coerce')
                    perc_df['error'] = pd.to_numeric(perc_df['error'], errors = 'coerce')
                    # Checking if we have a sum of 1
                    if abs(perc_df['rounded'].sum() - 1) <= tolerance:
                        rounded_before = perc_df['rounded'].iloc[0]
                        rounded_mid = perc_df['rounded'].iloc[1]
                        rounded_after = perc_df['rounded'].iloc[2]
                        break
        
    #The basic algorithm is: (don't round to the nearest percent before doing this)
    #Round everything down to the multiple of 5% below it. Throughout, refer to the "error" of a bin as the absolute value of the difference between the value assigned to the bin at this stage of the rounding process and its original value
    #Anything that's at 0%, round up to 5%
    #If the total is above 100%, remove 5% from the bin with lowest error that isn't currently assigned 5%. Repeat this step if needed.
    #If the total is below 100%, add 5% to the bin with lowest error among bins whose value is less than or equal to their original value. Repeat this step if needed.

        #add data to the intermediate csv
        # build human‐readable period label + start/end for *all* combos
        if date2 is None:
            # covers both “later” alone and “week4 + later” (ES1)
            period_label = f"after {date1.date()}"
            start_dt     = date1.date()
            end_dt       = None
        elif first_week == 'week1':
            # LS3
            period_label = f"before {date2.date()}"
            start_dt     = None
            end_dt       = date2.date()
        else:
            # any other single‐week (week2–week4) or (week2,week3) or (week3,week4) (ES3)
            period_label = f"between {date1.date()} and {date2.date()}"
            start_dt     = date1.date()
            end_dt       = date2.date()

        intermediate.append({
            'lon':            lon,
            'lat':            lat,
            'select_period':  first_week if second_week is None else f"{first_week}_{second_week}",
            'period_label':   period_label,
            'start_date':     start_dt,
            'end_date':       end_dt,
            'rounded_before': rounded_before,
            'rounded_mid':    rounded_mid,
            'rounded_after':  rounded_after
        })
    
        # Generate messages for each language
        # Restructured this to use a template instead of hardcoding languages (there are 9 languages)

        highest_bin = max(
            rounded_before or 0,
            rounded_mid    or 0,
            rounded_after  or 0
        )
        use_set2 = (highest_bin >= short_msg_cutoff)
        for lang in languages:
            # print(lang)
            ivrs = lang in ('Odi_ivrs', 'Eng_ivrs')      
            lang_ivrs = {'Odi_ivrs': 'Odi',              
                'Eng_ivrs': 'Eng'}.get(lang)
            if flag_val in ['NM', 'PM1', 'PM2']:
                sheet = flag_val + ('_IVRS' if ivrs else '')
                tmpl  = pd.read_excel(xlsx_path, sheet_name=sheet, engine='openpyxl', header=None)
                if flag_val == 'NM':
                    tmpl_name = 'Null Message'
                if flag_val == 'PM1':
                    tmpl_name = 'Promotional Message 1'
                if flag_val == 'PM2':
                    tmpl_name = 'Promotional Message 2'

            else:
                tmpl  = pick_template(first_week, second_week, ivrs, use_set2)
                tmpl_name = pick_template_name(first_week, second_week, ivrs, use_set2)
            lookup_lang = lang_ivrs if ivrs else lang


            cols = [str(x).strip().lower() for x in tmpl.iloc[0]]
            ci = cols.index(lookup_lang.lower())
            template = tmpl.iloc[1, ci]
            #next date -- forecasts go out day after they're produced


            next_date = forecast_date + timedelta(days=1)
            sms_day = str(next_date.day)
            mon_col = next(c for c in months_df.columns if c.lower()==lookup_lang.lower())
            sms_mon = months_df.loc[months_df['month_num']==next_date.month, mon_col].values[0]

            if flag_val in ['NM', 'PM1', 'PM2']:
                ci   = [c.strip().lower() for c in tmpl.iloc[0]].index(lookup_lang.lower())
                base = tmpl.iloc[1, ci]
                msg  = base.replace('<SMS Date>', sms_day).replace('<SMS Month>', sms_mon)
                messages.append({
                    'lon': lon,
                    'lat': lat,
                    'language': lang,
                    'forecast_message': msg,
                    'message_template': tmpl_name
                })
                continue

            # Compute percent values
            pct_mid    = int(round(rounded_mid   *100))
            if percent_before is not None:
                pct_before = int(round(rounded_before*100))
                q1 = get_qualifier(rounded_before*100, mon_col) # Qualitative tags for before
            else:
                pct_before = None
            if rounded_after is not None: 
                pct_after  = int(round(rounded_after *100))
                q2 = get_qualifier(rounded_after *100, mon_col) # Qualitative tags for after
            else:
                pct_after = None
    

            if tmpl is LS1_raw or tmpl is LS1_ivrs or tmpl is LS2_raw: #3 periods
                msg = (template
                    .replace('<SMS Date>', sms_day)
                    .replace('<SMS Month>', sms_mon)
                    .replace('<Date AA>', str(date1.day))
                    .replace('<Month B>', months_df.loc[months_df['month_num']==date1.month, mon_col].values[0])
                    .replace('<Date CC>', str(date2.day))
                    .replace('<Month D>', months_df.loc[months_df['month_num']==date2.month, mon_col].values[0])
                    .replace('XX', str(pct_mid))
                    .replace('YY', str(pct_before))
                    .replace('ZZ', str(pct_after))
                    .replace('<qual 1>', q1)
                    .replace('<qual 2>', q2)
                )
            elif tmpl is LS3_raw or tmpl is LS3_ivrs or tmpl is LS4_raw: #2 periods (selected period contains week1)
                #combined_before = percent_before + percent_mid
                #pct_comb = int(round(percent_mid*100))
                msg = (template
                    .replace('<SMS Date>', sms_day)
                    .replace('<SMS Month>', sms_mon)
                    .replace('<Date AA>', str(date2.day))
                    .replace('<Month B>', months_df.loc[months_df['month_num']==date2.month, mon_col].values[0])
                    .replace('XX', str(pct_mid))
                    .replace('YY', str(pct_after))
                    .replace('<qual 1>',     get_qualifier(rounded_mid * 100, mon_col))
                    .replace('<qual 2>',     get_qualifier(rounded_after * 100, mon_col))
                
                )
            else:  # ES1_raw, 2 periods (selected period contains "later")
                msg = (template
                    .replace('<SMS Date>', sms_day)
                    .replace('<SMS Month>', sms_mon)
                    .replace('<Date AA>', str(date1.day))
                    .replace('<Month B>', months_df.loc[months_df['month_num']==date1.month, mon_col].values[0])
                    .replace('XX', str(pct_mid))
                    .replace('YY', str(pct_before))
                    .replace('<qual 1>',     get_qualifier(rounded_mid * 100, mon_col))
                    .replace('<qual 2>',     get_qualifier(rounded_before * 100, mon_col))
                )

            messages.append({'lon': lon, 'lat': lat, 'language': lang,'message_template' : tmpl_name, 'forecast_message': msg})


    ######################
    # OUTPUT RESULTS
    ######################

    intermediate_out_path = out_path / "intermediate_period_probs.csv"
    logging.info(f"Writing intermediate output to {intermediate_out_path}")
    #output intermediate csv
    pd.DataFrame(intermediate).to_csv(
        intermediate_out_path,
        index=False,
        encoding='utf-8-sig'
    )


    #main output file
    out_df = grid_state[
        ['campaign_name','lon','lat','abbreviation','language']
    ].merge(
        pd.DataFrame(messages),
        left_on=['lon','lat','abbreviation'],
        right_on=['lon','lat','language'],
        how='inner',
        suffixes=('_grid','_msg')
    )
    # drop technical columns, keep only the grid_state.language, and remove lat/lon
    out_df = (
        out_df
        .drop(columns=['abbreviation','lon','lat','language_msg'])
        .rename(columns={'language_grid':'language'})
    )


    message_templates_out_path = out_path / "message_templates_output.xlsx"
    logging.info(f"Writing message templates output to {message_templates_out_path}")
    out_df.to_excel(message_templates_out_path, index=False, engine='openpyxl')




    ##############################
    #ENGLISH-ONLY OUTPUT CSV 
    ##############################
    # Copy for the forecast team to be able to read
    # Filter the in-memory messages for English…
    eng_msgs = [m for m in messages if m['language']=='Eng']
    eng_df   = pd.DataFrame(eng_msgs)

    # …then merge on lon/lat to get the campaign_name
    eng_out = (
        grid_state[['campaign_name','lon','lat']]
        .merge(eng_df[['lon','lat','message_template', 'forecast_message']], on=['lon','lat'], how='inner')
    )
    #we just want one output per lat/lon pair, since these are all in english
    eng_out = eng_out.groupby(['lon','lat'], as_index=False).first()

    message_templates_output_eng_path = out_path / "message_templates_output_eng.csv"
    logging.info(f"Writing English message templates output to {message_templates_output_eng_path}")
    eng_out.to_csv(
        message_templates_output_eng_path,
        index=False,
        encoding='utf-8-sig'
    )


    ##############################
    #ODISHA IVRS CSV 
    ##############################
    odi_ivrs_msgs = [m for m in messages if m['language']=='Odi_ivrs']
    odi_ivrs_df   = pd.DataFrame(odi_ivrs_msgs)

    # only keep those grid cells in Odisha
    grid_odisha = grid_state[grid_state['campaign_name'].str.contains('ODISHA', case=False, na=False)]

    odi_ivrs_out = grid_odisha.merge(
        odi_ivrs_df[['lon','lat', 'message_template', 'forecast_message']],
        on=['lon','lat'],
        how='inner'
    )

    odi_ivrs_out_path = out_path / "message_templates_output_odi_ivrs.xlsx"
    logging.info(f"Writing Odisha IVRS message templates output to {odi_ivrs_out_path}")
    odi_ivrs_out.to_excel(
        odi_ivrs_out_path,
        index=False,
        engine='openpyxl'
    )

    ##############################
    #English IVRS CSV 
    ##############################
    eng_ivrs_msgs = [m for m in messages if m['language']=='Eng_ivrs']
    eng_ivrs_df   = pd.DataFrame(eng_ivrs_msgs)

    eng_ivrs_out = grid_odisha.merge(
        eng_ivrs_df[['lon','lat', 'message_template', 'forecast_message']],
        on=['lon','lat'],
        how='inner'
    )
    eng_ivrs_out_path = out_path / "message_templates_output_odi_eng_ivrs.xlsx"
    eng_ivrs_out.to_excel(
        eng_ivrs_out_path,
        index=False,
        engine='openpyxl'
    )

    return eng_out

#!/bin/csh

set year = 2025

# Loop over all days of the year using bash's date command
foreach date (`bash -c 'for d in {0..5}; do date -d "2025-05-04 +$d day" +%Y%m%d; done'`)
    set yyyy = `echo $date | cut -c1-4`
    set mm   = `echo $date | cut -c5-6`
    set dd   = `echo $date | cut -c7-8`

    echo "Downloading $yyyy-$mm-$dd"
    set fname = "../raw/IMERG_daily/3B-DAY-L.MS.MRG.3IMERG.${yyyy}${mm}${dd}-S000000-E235959.V07B.nc4"
    if (-e "$fname") then
        echo "Already exists: $fname"
        continue
    endif

    wget --load-cookies ../.auth/.urs_cookies --save-cookies ../.auth/.urs_cookies --keep-session-cookies --content-disposition "https://gpm1.gesdisc.eosdis.nasa.gov/data/GPM_L3/GPM_3IMERGDL.07/${yyyy}/${mm}/3B-DAY-L.MS.MRG.3IMERG.${yyyy}${mm}${dd}-S000000-E235959.V07B.nc4" -P ../raw/IMERG_daily
end
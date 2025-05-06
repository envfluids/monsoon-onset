#!/bin/csh

#Format strings
set year = `date -d yesterday +%Y`
set month = `date -d yesterday +%m`
set day = `date -d yesterday +%d`

set prevyear = `date -d '2 days ago' +%Y`
set prevmonth = `date -d '2 days ago' +%m`
set prevday = `date -d '2 days ago' +%d`

echo "${prevday} ${day}"
#GET request to send for IMERG cookies - see readme for troubleshooting issues
wget --load-cookies ../.auth/.urs_cookies --save-cookies ../.auth/.urs_cookies --keep-session-cookies --content-disposition "https://gpm1.gesdisc.eosdis.nasa.gov/data/GPM_L3/GPM_3IMERGDL.07/${year}/${month}/3B-DAY-L.MS.MRG.3IMERG.${year}${month}${day}-S000000-E235959.V07B.nc4" -P ../raw/IMERG_daily

#Looks at previous day's data and sees if its downloaded (chance its not due to some Perlmutter outages). If it isn't then it downloads that day's data as well
#global/cfs/cdirs/m3310/tyang25/IMERG_daily/3B-DAY-L.MS.MRG.3IMERG.20250413-S000000-E235959.V07B.nc4
if (! -e "../raw/IMERG_daily/3B-DAY-L.MS.MRG.3IMERG.${prevyear}${prevmonth}${prevday}-S000000-E235959.V07B.nc4") then
    wget --load-cookies ../.auth/.urs_cookies --save-cookies ../.auth/.urs_cookies --keep-session-cookies --content-disposition "https://gpm1.gesdisc.eosdis.nasa.gov/data/GPM_L3/GPM_3IMERGDL.07/${prevyear}/${prevmonth}/3B-DAY-L.MS.MRG.3IMERG.${prevyear}${prevmonth}${prevday}-S000000-E235959.V07B.nc4" -P ../raw/IMERG_daily
    echo "Yesterday's File doesn't exist, downloading"
else

    echo "Yesterday's File does exist"
endif
python ./indian_monsoon_script.py
# python IMERG_daily/imerg_plot.py
# python IMERG_daily/imerg_plot2.py
# echo "Finished script!"
# scp -o StrictHostKeyChecking=no /global/cfs/cdirs/m3310/tyang25/5DCamPrecip/5Dprecip${year}${month}${day}.jpg worldmon@worldmonsoons.org:~/public_html/images/CameroonImergImages/5Dpreciplatest.jpg

# scp -o StrictHostKeyChecking=no /global/cfs/cdirs/m3310/tyang25/5DTSCamPrecip/5DTSprecip${year}${month}${day}.jpg worldmon@worldmonsoons.org:~/public_html/images/CameroonImergImages/5DTSpreciplatest.jpg

# scp -o StrictHostKeyChecking=no /global/cfs/cdirs/m3310/tyang25/AccumulatedIMERG/accumCam_${year}${month}${day}.jpg worldmon@worldmonsoons.org:~/public_html/images/CameroonImergImages/accumCamlatest.jpg

# scp -o StrictHostKeyChecking=no /global/cfs/cdirs/m3310/tyang25/AccumIntegral/accumCamint_${year}${month}${day}.jpg worldmon@worldmonsoons.org:~/public_html/images/CameroonImergImages/accumCamintlatest.jpg
echo "Finished copying!"
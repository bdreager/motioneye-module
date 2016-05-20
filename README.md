# motioneye-module
Enhancement script for motioneye

###What
A small script meant to run as a cron job to compliment [motioneye](https://github.com/ccrisan/motioneye)

###Features
* Automagically detect settings from motioneye config file(s) (limited support)
* Delete files from Dropbox that have been deleted (by motioneye) locally
* Generate timelapse videos from saved images for all completed days (only for a single device at the moment)
* Upload timelapse videos to Dropbox
* Delete old timelapse videos, both locally and on Dropbox (based on motioneye's preserve_pictures setting multiplied by 2)
* Logs from the last run are saved to motioneye's logs directory

###Requires
[FFmpeg](https://ffmpeg.org/), [Dropbox python SDK](https://www.dropbox.com/developers-v1/core/sdks/python), and probably a few other things I'm forgetting.

###Disclaimer 
This script was really only designed for my system and its specific configuration. YMMV

###Other
The convoluted method I'm using to generate timelapses was nessesary for reliability reasons on low-end hardware.

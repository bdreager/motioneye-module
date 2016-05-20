#!/usr/bin/env python
# -*- coding: utf-8 -*-

__program__ = 'meyem'
__version__ = '1.0.0'

import dropbox, os, json, re, subprocess, time, logging, shutil, uuid
from argparse import ArgumentParser
from datetime import datetime, date, timedelta
from tendo import singleton
singleton.SingleInstance() # this script will likely be a chron job, se we only want one to run at a time

log = logging.getLogger(__name__)

def init_args():
    parser = ArgumentParser()
    parser.add_argument('--debug', action='store_true', default=False, dest='debug')
    return parser.parse_args()

def basename(path):
    return os.path.basename(os.path.normpath(path))

def update_remote(client, remote, local):
    local_basenames = [basename(path) for path in local]
    for file in remote:
        if not basename(file) in local_basenames:
            log.info('Deleting [{}]'.format(basename(file)))
            client.file_delete(file)
        else:
            log.info('Preserving [{}]'.format(basename(file)))

def delete_old_local(files, cutoff_date):
    pattern = re.compile(FileDater.kNAME_DATE_PATTERN)

    for file in files:
        file_basename = os.path.splitext(basename(file))[0]
        file_date = FileDater.datename(file_basename) if pattern.match(file_basename) else FileDater.lastmod(file)
        if file_date < cutoff_date:
            log.info('{} older than {}, deleting {}'.format(file_date, cutoff_date, basename(file)))
            os.remove(file)

def upload_timelapses(client, local_files, remote_files, remote_dest):
    for i, file_path in enumerate(local_files, 1):
        file_basename = basename(file_path)
        log.info('Uploading [{}/{}] {}'.format(i,len(local_files), file_basename))
        if file_basename in remote_files:
            log.info('\tremote file exists, skipping')
        else:
            with open(file_path, 'rb') as file:
                full_dest = os.path.join(remote_dest,file_basename)
                response = client.put_file(full_dest, file)
                log.info('\tdone')

# this one did not work for some reason, likely a strange network issue
def upload_file(client, file_path, chunk_size, dest):
    with open(file_path, 'rb') as file:
        size = os.path.getsize(file_path)
        uploader = client.get_chunked_uploader(file, size)
        while uploader.offset < size:
            upload = uploader.upload_chunked(chunk_size=chunk_size)
        uploader.finish(dest)

def safemakedirs(path):
    try:    os.makedirs(path)
    except: pass

def chunk_list(list, chunk_size):
    return [list[i:i + chunk_size] for i in range(0, len(list), chunk_size)]

class Config(object):
    sysvinit_conf = '/etc/init.d/motioneye'
    upload_conf = 'uploadservices.json'
    device_conf = 'thread-1.conf'

    def __init__(self):
        #find motioneye config file
        with open(self.sysvinit_conf) as file:
            file_contents = file.read()
            motioneye_conf = re.search('OPTIONS.*[\s|"](\/\S*\w)[\s|"]', file_contents).group(1)

        #get paths from motioneye config
        with open(motioneye_conf) as file:
            file_contents = file.read()

            motioneye_path = re.search("run_path\s(.+)", file_contents).group(1)
            log_path = re.search("log_path\s(.+)", file_contents).group(1)
            self.log_file = os.path.join(log_path, __program__+'.log')
            safemakedirs(log_path)

        #get dropbox settings
        db_conf_path = os.path.join(motioneye_path, self.upload_conf)
        with open(db_conf_path) as file: db_conf = json.load(file)
        self.remote_backups_path = db_conf['1']['dropbox']['location']
        self.db_auth_token = db_conf['1']['dropbox']['credentials']['access_token']

        #device settings
        #TODO handle multiple devices
        motioneye_device_conf = os.path.join(motioneye_path, self.device_conf)
        with open(motioneye_device_conf) as file:
            file_contents = file.read()
            self.local_backups_path = re.search("target_dir\s(.+)", file_contents).group(1)
            preserve_pictures = re.search("preserve_pictures\s(.+)", file_contents).group(1)

        #timelapse settings
        self.preserve_timelapses = int(int(preserve_pictures) * 2)
        self.timelapses_basename = 'timelapses'
        self.local_timelapses_path = os.path.join(self.local_backups_path, self.timelapses_basename)
        self.remote_timelapses_path = os.path.join(self.remote_backups_path, self.timelapses_basename)

class Timelapser(object):
    kTIMELAPSE_EXT = '.mp4'
    kTIMELAPSE_IMG_COM = 'ffmpeg -n -framerate {} -f image2pipe -vcodec mjpeg -i - -c:v libx264 -profile:v main -level:v 4.1 -preset medium {}'
    kTIMELAPSE_VID_COM = 'ffmpeg -safe 0 -f concat -i {} -c copy {}'

    kBATCH_SECONDS = 5

    kTIMELAPSE_FRAMERATE = 30

    def __init__(self, timelapses_location):
        self.timelapses_location = timelapses_location
        safemakedirs(self.timelapses_location)

    def generate_timelapses(self, images_dirs):
        cur_date = FileDater.datenow()
        pattern = re.compile(FileDater.kNAME_DATE_PATTERN)
        for dir in images_dirs:
            dir_basename = basename(dir)
            if os.path.isdir(dir):
                log.info('Working on {}...'.format(dir_basename))
                # mod date can be wrong, try to use folder name if format is correct
                dir_date = FileDater.datename(dir_basename) if pattern.match(dir_basename) else FileDater.lastmod(dir)

                if not dir_date < cur_date:
                    log.info('\tstill active, skipping')
                    continue

                dest_file = os.path.join(self.timelapses_location, dir_basename+self.kTIMELAPSE_EXT)
                if os.path.isfile(dest_file):
                    log.info('\talready exists, skipping')
                    continue

                self.generate_timelapse(dir, dest_file)
                log.info('\tdone')

    def generate_timelapse(self, source_dir, dest_file):
        tmp_folder = os.path.join(source_dir, 'tmp')
        safemakedirs(tmp_folder)

        files = [os.path.join(source_dir, file) for file in os.listdir(source_dir) if file.endswith('.jpg')]
        if len(files) == 0:
            log.info('\tno files')
            return
        files.sort()

        video_chunks = []
        batch_size = self.kTIMELAPSE_FRAMERATE * self.kBATCH_SECONDS
        batches = chunk_list(files, batch_size)
        source_basename = basename(source_dir)
        log.info('\tgenerating {} video(s) from {} image(s)...'.format(len(batches), len(files)))
        for i, batch in enumerate(batches, 1):
            tmp_dest = os.path.join(tmp_folder, '{}_{}{}'.format(source_basename, i, self.kTIMELAPSE_EXT))
            if not os.path.isfile(tmp_dest):
                log.debug('\t\t[{}/{}] {}'.format(i, len(batches), tmp_dest))
                cat_com = "cat {}".format(" ".join(batch))
                ffmpeg_com = self.kTIMELAPSE_IMG_COM.format(self.kTIMELAPSE_FRAMERATE, tmp_dest)
                command = "{} | {}".format(cat_com, ffmpeg_com)
                pipe = subprocess.call(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=None)
            video_chunks.append(tmp_dest)

        log.info('\tcreating {}...'.format(basename(dest_file)))
        list_path = os.path.join(tmp_folder, str(uuid.uuid4()))
        list_string = ''.join(["file '{}'\n".format(path) for path in video_chunks])
        #for path in video_chunks: list_string += "file '{}'\n".format(path)
        with open(list_path, "w") as file: file.write(list_string)

        command = self.kTIMELAPSE_VID_COM.format(list_path, dest_file)
        pipe = subprocess.call(command, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=None)
        shutil.rmtree(tmp_folder)

class FileDater(object):
    kFORMAT = '%Y%m%d'
    kNAME_DATE_PATTERN = "^\d{4}-\d{2}-\d{2}$"

    @staticmethod
    def lastmod(thing):
        return int(time.strftime(FileDater.kFORMAT, time.localtime(os.path.getmtime(thing))))

    @staticmethod
    def datename(thing):
        return int(re.sub('-', '', thing)) # this assumes the name is YYYY-MM-DD'

    @staticmethod
    def datenow():
        return int(datetime.now().strftime(FileDater.kFORMAT))

    @staticmethod
    def dateago(days):
        return FileDater.datename(str(date.today()-timedelta(days=days)))

if __name__ == '__main__':
    args = init_args()
    config = Config()

    logging.basicConfig(filename=config.log_file, filemode='w', format='%(levelname)s: %(message)s',level=logging.DEBUG)
    if args.debug: log.addHandler(logging.StreamHandler()) # print logs to console when debugging

    #TODO upgrade to Dropbox API v2
    client = dropbox.client.DropboxClient(config.db_auth_token)
    remote_metadata = client.metadata(config.remote_backups_path, list=True)

    local_backups = [os.path.join(config.local_backups_path, path) for path in os.listdir(config.local_backups_path) if path not in config.timelapses_basename]
    local_backups.sort()
    remote_backups = [content['path'] for content in remote_metadata['contents'] if config.remote_timelapses_path not in content['path']]
    log.info('\n==========> Updating remote backups')
    update_remote(client, remote_backups, local_backups)

    timelapser = Timelapser(config.local_timelapses_path)
    log.info('\n==========> Generating timelaspses')
    timelapser.generate_timelapses(local_backups)

    local_timelapse_files = [os.path.join(config.local_timelapses_path, path) for path in os.listdir(config.local_timelapses_path)]
    local_timelapse_files.sort()

    #TODO change remote_timelapse_files to use full paths, and make sure it all still works
    remote_metadata = client.metadata(config.remote_timelapses_path, list=True)
    remote_timelapse_files = [basename(content['path']) for content in remote_metadata['contents']]
    log.info('\n==========> Uploading timelapses')
    upload_timelapses(client, local_timelapse_files, remote_timelapse_files, config.remote_timelapses_path)

    #NOTE rebuild remote_timelapse_files after uploading, since it might have changed
    remote_metadata = client.metadata(config.remote_timelapses_path, list=True)
    remote_timelapse_files = [os.path.join(config.remote_timelapses_path, basename(content['path'])) for content in remote_metadata['contents']]

    cutoff = FileDater.dateago(config.preserve_timelapses)
    log.info('\n==========> Deleting old timelapses locally')
    delete_old_local(local_timelapse_files, cutoff)

    #NOTE rebuild local_timelapse_files after deleting, since it might have changed
    local_timelapse_files = [os.path.join(config.local_timelapses_path, path) for path in os.listdir(config.local_timelapses_path)]
    local_timelapse_files.sort()
    log.info('\n==========> Updating remote timelapses')
    update_remote(client, remote_timelapse_files, local_timelapse_files)

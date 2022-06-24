#
#   PubMed download updater
#


from trialstreamer import dbutil, config
from trialstreamer.readers import pmreader
import trialstreamer
import logging
import os
import re
import datetime
from dateutil import parser
import hashlib
import tqdm
import xml.etree.cElementTree as ET
import subprocess
import sys
import json
import gzip
import ftplib
import glob
import psycopg2
import collections
from itertools import zip_longest
from psycopg2.extras import execute_values
import requests
import time



with open(os.path.join(trialstreamer.DATA_ROOT, 'rct_model_calibration.json'), 'r') as f:
    clf_cutoffs = json.load(f)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

homepage = "ftp.ncbi.nlm.nih.gov"
max_retry_attempts = config.DOWNLOAD_RETRY_ATTEMPTS or 1


def get_ftp():
    """
    log in to FTP and pass back
    """
    ftp = ftplib.FTP(homepage)
    print(ftp.login(user="anonymous", passwd=config.PUBMED_USER_EMAIL))
    # ftp.set_pasv(False)

    return ftp


def get_baseline_fns():
    ftp = get_ftp()
    filelist = ftp.nlst('pubmed/baseline/')
    baseline_fns = [f for f in filelist if f.endswith('.gz')]
    return baseline_fns


def get_daily_update_fns():
    ftp = get_ftp()
    filelist = ftp.nlst('pubmed/updatefiles/')
    update_fns = [f for f in filelist if f.endswith('.gz')]
    update_fns.sort() # since we want to process these in order
    return update_fns


def get_daily_update_mod_times(update_fns):
    ftp = get_ftp()
    out = {}
    for fn in update_fns:
        out[os.path.basename(fn)] = parser.parse(ftp.sendcmd('MDTM {}'.format(fn))[4:].strip())
    return out


def get_daily_update_fn_and_times():
    ftp = get_ftp()
    filelist = ftp.mlsd('pubmed/updatefiles/')
    update_fns, modtimes = [], {}

    for r in filelist:
        if r[0].endswith('.gz'):
            update_fns.append(os.path.join('pubmed/updatefiles/', r[0]))
            modtimes[r[0]] = parser.parse(r[1]['modify'])
    return update_fns, modtimes


def already_done_md5s(updates=False):
    """
    glob which md5s already downloaded
    """
    if updates:
        pth = os.path.join(config.PUBMED_LOCAL_DATA_PATH, 'updates', '*.md5')
    else:
        pth = os.path.join(config.PUBMED_LOCAL_DATA_PATH, '*.md5')
    fns = glob.glob(pth)
    return set([os.path.basename(r) for r in fns])


def already_done_gzs(updates=False):
    """
    glob which gzipped data files already downloaded
    """
    if updates:
        pth = os.path.join(config.PUBMED_LOCAL_DATA_PATH, 'updates', '*.gz')
    else:
        pth = os.path.join(config.PUBMED_LOCAL_DATA_PATH, '*.gz')

    fns = glob.glob(pth)
    return set([os.path.basename(r) for r in fns])


def already_done_updates():
    cur = dbutil.db.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("SELECT source_filename FROM update_log WHERE update_type='pubmed_update';")
    already_done_files = set((r['source_filename'] for r in cur.fetchall()))
    return already_done_files


def download_ftp_updates():
    """
    Grab the updates

    """
    log.info("Obtaining daily updates from PubMed")
    if not dbutil.last_update(update_type='pubmed_baseline'):
        log.warning("No baseline files in database; stopping. Run download_ftp_baseline() before updates")
        return None

    # download all available update files

    log.info('getting the filenames and times')
    update_ftp_fns, update_ftp_mod_times = get_daily_update_fn_and_times()
    # log.info('getting the file dates')
    # update_ftp_mod_times = get_daily_update_mod_times(update_ftp_fns)

    log.info("{} PubMed title/abstract update files on FTP server".format(len(update_ftp_fns)))
    log.info("Checking hashfiles, and downloading any missing")

    already_done_fns = already_done_gzs(updates=True)
    log.info(f"{len(already_done_fns)} already done gz files")
    log.info("Verifying local gzipped data files")
    validate_downloaded_data(already_done_fns, updates=True)
    log.info("Data validated")

    log.info("Checking hashfiles, and downloading any missing")

    download_md5s(update_ftp_fns, updates=True)
    download_and_validate_gzs(update_ftp_fns, updates=True)
    log.info("Uploading to postgres")
    already_done_fns = already_done_gzs(updates=True)
    log.info(f"{len(already_done_fns)} valid gz files")

    safety_test_parse = config.SAFETY_TEST_PARSE
    upload_to_postgres(update_ftp_fns, safety_test_parse=safety_test_parse, batch_size=5000, updates=True, modtimes=update_ftp_mod_times)

    log.info("Uploaded!")
    # log.info("Refreshing counts")
    # update_counts()
    log.info("All done successfully!")


def update_counts():
    cur = dbutil.db.cursor()
    cur.execute("refresh materialized view pubmed_rct_count;")
    cur.close()
    dbutil.db.commit()


def download_ftp_baseline(force_update=False):
    """
    Grab all the latest baseline files (checking if already done first)

    edit safety_test_parse in config file for deployment

    """
    log.info("Checking baseline data from PubMed")
    if dbutil.last_update(update_type='pubmed_baseline') and force_update==False:
        log.warning("Baseline files already in database, no action will be taken. To delete database and start again rerun with force_update=True")
        return None

    safety_test_parse = config.SAFETY_TEST_PARSE
    if not safety_test_parse:
        log.warning("NB The parse is not being test run... This is quicker, but unexpected code crashes will result in local database being lost")

    baseline_ftp_fns = get_baseline_fns()
    log.info("{} PubMed title/abstract files on FTP server".format(len(baseline_ftp_fns)))
    log.info("Checking hashfiles, and downloading any missing")
    download_md5s(baseline_ftp_fns)
    log.info("Verifying local gzipped data files")
    already_done_fns = already_done_gzs()

    # validate_downloaded_data(already_done_fns)

    log.info("Data validated")
    log.info("Downloading and validating remaining files")
    download_and_validate_gzs(baseline_ftp_fns)
    download_date = datetime.datetime.now()
    log.info("Uploading to postgres")
    upload_to_postgres(baseline_ftp_fns, safety_test_parse, force_update=force_update)
    log.info("Uploaded!")
    # log.info("Refreshing counts")
    # update_counts()
    log.info("All done successfully!")

    dbutil.log_update(update_type='pubmed_baseline', source_filename=os.path.basename(baseline_ftp_fns[0])[:8], source_date=get_date_from_fn(baseline_ftp_fns[0]), download_date=download_date)


def download_md5s(gz_fns, updates=False):
    """
    get the hashes from a list of PubMed gziped filenames
    """
    already_done = already_done_md5s(updates=updates)
    ftp = get_ftp()
    for i, gz_fn in enumerate(tqdm.tqdm(gz_fns, desc='md5 hashes downloaded from PubMed FTP server')):
        if os.path.basename(gz_fn) + ".md5" in already_done:
            # skip the already downloaded hashes
            continue
        if updates:
            out_filename = os.path.join(config.PUBMED_LOCAL_DATA_PATH, 'updates', os.path.basename(gz_fn))
        else:
            out_filename = os.path.join(config.PUBMED_LOCAL_DATA_PATH, os.path.basename(gz_fn))
        attempt = 0
        while attempt <= max_retry_attempts:
            try:
                log.info(f'Downloading {out_filename+".md5"}... ')
                with open(out_filename + ".md5", 'wb') as f:
                    ftp.retrbinary('RETR ' + gz_fn + ".md5", f.write)
                break
            except Exception:
                attempt += 1
                log.info(f'Failed, retrying download ({attempt})...')
                # Delete corrupted file
                if os.path.exists(out_filename + ".md5"):
                    os.remove(out_filename + ".md5")


def download_and_validate_gzs(gz_fns, updates=False):
    """
    download datafiles, and validate
    if updates=True, saves to the updates local folder
    """
    already_done = already_done_gzs(updates=updates)

    for i, gz_fn in enumerate(tqdm.tqdm(gz_fns, desc='data files downloaded from PubMed FTP server')):
        if os.path.basename(gz_fn) in already_done:
            # skip the already downloaded files
            continue

        if updates:
            out_filename = os.path.join(config.PUBMED_LOCAL_DATA_PATH, 'updates', os.path.basename(gz_fn))
        else:
            out_filename = os.path.join(config.PUBMED_LOCAL_DATA_PATH, os.path.basename(gz_fn))

        ftp = get_ftp()

        attempt = 0
        while attempt <= max_retry_attempts:
            try:
                log.info(f'Downloading {out_filename}...')
                with open(out_filename, 'wb') as f:
                    ftp.retrbinary('RETR ' + gz_fn, f.write)
                with open(out_filename + ".md5", 'wb') as f:
                    ftp.retrbinary('RETR ' + gz_fn + ".md5", f.write)
                log.info(f'Validating downloaded file {out_filename}')
                validate_file(out_filename, out_filename + ".md5", raise_for_errors=True)
                break

            except Exception:
                attempt += 1
                log.info(f'Failed, retrying download ({attempt})...')
                # Delete corrupted files
                if os.path.exists(out_filename):
                    os.remove(out_filename)
                if os.path.exists(out_filename + ".md5"):
                    os.remove(out_filename + ".md5")


def validate_downloaded_data(fns, updates=False):
    for fn in tqdm.tqdm(fns, desc="Validating local files"):
        if updates:
            fn_path = os.path.join(config.PUBMED_LOCAL_DATA_PATH, 'updates', fn)
        else:
            fn_path = os.path.join(config.PUBMED_LOCAL_DATA_PATH, fn)
        validate_file(fn_path, fn_path+".md5", raise_for_errors=True)
    return True


def validate_file(fn, hash_fn, raise_for_errors=True):

    with open(fn, 'rb') as f:
        bn = f.read()
    obs_md5 = hashlib.md5(bn).hexdigest()
    with open(hash_fn, 'r') as f:
        true_md5 = (f.read()).rstrip().split("= ")[-1]
    if obs_md5 != true_md5 and raise_for_errors:
        raise Exception("File {} doesn't match md5... possibly corrupted, suggest delete and redownload".format(fn))
    else:
        return obs_md5 == true_md5


def iter_abstracts(fn, updates=False, skip_list=None):
    if skip_list is None:
        skip_list = set()
    with open(fn, 'rb') as f:
        decompressedFile = gzip.GzipFile(fileobj=f, mode='r')
        for event, elem in ET.iterparse(decompressedFile, events=("start", "end")):
            if elem.tag == "MedlineCitation" and event=="end":
                if elem.find('PMID').text in skip_list:
                    continue
                p_article = pmreader.PubmedCorpusReader(xml_ET=elem)
                if not updates:
                    yield p_article.to_dict()
                else:
                    yield {"action": "update", "article": p_article.to_dict()}
            if elem.tag == "DeleteCitation" and event=="end" and updates:
                yield {"action": "delete_list", "pmids": [r.text for r in elem]}


def predict(X, tasks=None, filter_rcts="is_rct_sensitive"):

    if tasks is None:
        tasks = ['rct_bot']
    base_url = config.ROBOTREVIEWER_URL
    upload_data = {
        "articles": X,
        "robots": tasks,
        "filter_rcts": filter_rcts
    }
    r = requests.post(base_url+'queue-documents', json=upload_data, headers={"api-key": trialstreamer.config.ROBOTREVIEWER_API_KEY})
    response = r.json()
    if 'report_id' not in response:
        raise Exception(f'Invalid response for RobotReviewer API request: {response}')
    report_id = response['report_id']
    log.info(f'Queued documents with report ID: {report_id}')

    def check_report(report_id):
        r = requests.get(base_url +'report-status/'+report_id, headers={"api-key": trialstreamer.config.ROBOTREVIEWER_API_KEY})
        return r.json()['state'] == 'SUCCESS'

    while not check_report(report_id):
        time.sleep(0.3)

    report = requests.get(base_url +'report/'+report_id, headers={"api-key": trialstreamer.config.ROBOTREVIEWER_API_KEY}).json()
    return report


def classify(entry_batch):

    global clf_cutoffs

    thresholds_ptyp = clf_cutoffs['thresholds']['svm_cnn_ptyp']
    thresholds_no_ptyp = clf_cutoffs['thresholds']['svm_cnn']

    threshold_types = ["precise", "balanced", "sensitive"]


    X = []

    for entry in entry_batch:
        row = {"ti": entry['title'], "ab": entry['abstract_plaintext'], "ptyp": entry['ptyp']}
        if entry['status'] == 'MEDLINE' and entry['indexing_method'] != 'Automated':
            # https://www.nlm.nih.gov/pubs/techbull/ja18/ja18_indexing_method.html
            # new addition
            # we will use either fully manual, or manually corrected ('Curated') ptyps, but ignore any fully automated
            pass
        else:
            row.pop('ptyp', None)
        X.append(row)

    preds = predict(X, tasks=['rct_bot', 'human_bot'], filter_rcts='none')
    # prepare data out

    out = []

    for pred in preds:

        row = {"clf_type": pred["rct_bot"]["model"], "clf_score": pred["rct_bot"]['score'], "clf_date": datetime.datetime.now(), "ptyp_rct": pred["rct_bot"]['ptyp_rct'],
        "score_cnn": pred["rct_bot"]["preds"]["cnn"], "score_svm": pred["rct_bot"]["preds"]["svm"], "score_svm_cnn": pred["rct_bot"]["preds"]["svm_cnn"], "score_svm_ptyp": pred["rct_bot"]["preds"]["svm_ptyp"],
        "score_cnn_ptyp": pred["rct_bot"]["preds"]["cnn_ptyp"], "score_svm_cnn_ptyp": pred["rct_bot"]["preds"]["svm_cnn_ptyp"], "rct_probability": pred["rct_bot"]["preds"]["probability"], "is_human": pred["human_bot"]["is_human"]}

        if pred["rct_bot"]["model"] == "svm_cnn_ptyp":
            for tt in threshold_types:
                row['is_rct_{}'.format(tt)] = (pred["rct_bot"]['score'] >= thresholds_ptyp[tt])
        elif pred["rct_bot"]["model"] == "svm_cnn":
            for tt in threshold_types:
                row['is_rct_{}'.format(tt)] = (pred["rct_bot"]['score'] >= thresholds_no_ptyp[tt])
        out.append(row)
    return out


def grouper(iterable, n, fillvalue=None):
    "Collect data into fixed-length chunks or blocks - from itertools recipes adapted a bit"
    # grouper('ABCDEFG', 3, 'x') --> ABC DEF Gxx"
    args = [iter(iterable)] * n
    return ([i for i in r if i is not None] for r in zip_longest(*args, fillvalue=fillvalue))


def get_date_from_fn(ftp_fn):
    """
    The 2018 baseline runs until 31st December 2017
    """
    bn = os.path.basename(ftp_fn)
    return datetime.datetime(int("20" + bn[6:8])-1, 12, 31)


def upload_to_postgres(ftp_fns, safety_test_parse, batch_size=5000, force_update=False, updates=False, modtimes=None):
    """
    ftp_fns = the filenames to parse (converted to local fns here)
    safety_test_parse = recommended, try and read all the abstracts before deleting the existing data
    batch_size = how many to do at once (often lower = fatster)
    force_update = whether to delete the database
    """

    num_files = len(ftp_fns)

    if safety_test_parse:
        log.info("Testing parse before inserting into database")
        for idx, ftp_fn in enumerate(ftp_fns):
            if updates:
                local_fn = os.path.join(config.PUBMED_LOCAL_DATA_PATH, 'updates', os.path.basename(ftp_fn))
            else:
                local_fn = os.path.join(config.PUBMED_LOCAL_DATA_PATH, os.path.basename(ftp_fn))
            for entry in tqdm.tqdm(iter_abstracts(local_fn, updates=updates), desc="testing the abstract parsing ({}/{}) {}".format(idx, num_files, local_fn)):
                if updates:
                    if entry['action'] == 'update':
                        _ = (entry['article']['pmid'], entry['article']['year'], entry['article']['title'], entry['article']['abstract_plaintext'], json.dumps(entry['article']), ftp_fn)
                    elif entry['action'] == 'delete_list':
                        _ = entry['pmids']
                else:
                    _ = (entry['pmid'], entry['year'], entry['title'], entry['abstract_plaintext'], json.dumps(entry), ftp_fn)



    # if safety mode only delete existing database where the parse has completed without exception
    if updates==False:
        if force_update:
            log.warning("Deleting all entries from PubMed database")
            cur = dbutil. db.cursor()
            cur.execute("DELETE FROM pubmed;")
            cur.close()
            dbutil.db.commit()
            already_done_pmids = set()
            # dangerous command....
        else:
            # get already done PMIDs
            cur = dbutil.db.cursor(cursor_factory=psycopg2.extras.DictCursor)
            cur.execute("SELECT pmid FROM pubmed;")
            already_done_pmids = set((r['pmid'] for r in cur.fetchall()))
            cur.execute("SELECT pmid FROM pubmed_excludes;")
            already_done_pmids.update((r['pmid'] for r in cur.fetchall()))
            log.warning("will skip {} already done... rerun with 'force_update=True' to reclassify all".format(len(already_done_pmids)))
    else:
        already_done_pmids = set()



    stats = collections.Counter()

    if updates:
        logged_completed_fns = already_done_updates()

    for idx, ftp_fn in enumerate(ftp_fns):

        if updates:
            bn = os.path.basename(ftp_fn)
            if bn in logged_completed_fns:
                continue
            local_fn = os.path.join(config.PUBMED_LOCAL_DATA_PATH, 'updates', bn)
        else:
            local_fn = os.path.join(config.PUBMED_LOCAL_DATA_PATH, os.path.basename(ftp_fn))


        for entry_batch in tqdm.tqdm(grouper(iter_abstracts(local_fn, updates=updates, skip_list=already_done_pmids), batch_size), desc="classifying and uploading postgres ({}/{}) {}".format(idx, num_files, local_fn)):

            include_rows = []
            exclude_rows = []


            stats["batches classified"] += 1


            if updates:

                pmids_to_delete = []
                for r in entry_batch:
                    if r['action']=='delete_list':
                        pmids_to_delete.extend(r['pmids'])

                entry_batch = [r['article'] for r in entry_batch if r['action']=='update']

            # reverse the list and
            # remove any duplicates
            # (so that the later entries are added only)
            dedupe = []
            enc = set()

            while entry_batch:
                r = entry_batch.pop()
                if r['pmid'] not in enc:
                    dedupe.append(r)
                    enc.add(r['pmid'])

            entry_batch, dedupe = dedupe, entry_batch
            if len(entry_batch) == 0:
                continue

            preds = classify(entry_batch)

            for entry, pred in zip(entry_batch, preds):

                year = int(entry['year']) if entry['year'] else None

                # need to ignore entries without PMIDs
                if not entry['pmid']:
                    continue
                timestamp = datetime.datetime.now()
                if pred['is_rct_sensitive']:
                    row = (entry['pmid'], entry['status'], year, entry['title'], entry['abstract_plaintext'],
                        json.dumps(entry), ftp_fn, pred['clf_type'], pred['clf_score'], pred['clf_date'], pred['ptyp_rct'], pred['is_rct_precise'],
                        pred['is_rct_balanced'], pred['is_rct_sensitive'], entry['indexing_method'], pred['score_svm'], pred['score_cnn'], pred['score_svm_cnn'],
                        pred['score_svm_ptyp'], pred['score_cnn_ptyp'], pred['score_svm_cnn_ptyp'], pred['rct_probability'], pred['is_human'], timestamp)

                    include_rows.append(row)
                else:

                    row = (entry['pmid'], entry['status'], year,
                        ftp_fn, pred['clf_type'], pred['clf_score'], pred['clf_date'], pred['ptyp_rct'], pred['is_rct_precise'],
                        pred['is_rct_balanced'], pred['is_rct_sensitive'], entry['indexing_method'], pred['score_svm'], pred['score_cnn'], pred['score_svm_cnn'],
                        pred['score_svm_ptyp'], pred['score_cnn_ptyp'], pred['score_svm_cnn_ptyp'], pred['rct_probability'], pred['is_human'], timestamp)
                    exclude_rows.append(row)

            cur = dbutil. db.cursor()
            if updates:
                for pm in pmids_to_delete:
                    cur.execute("DELETE FROM pubmed WHERE pmid=(%s);", (pm,))
                    cur.execute("DELETE FROM pubmed_excludes WHERE pmid=(%s);", (pm,))
                    cur.execute("DELETE FROM pubmed_annotations WHERE pmid=(%s);", (pm, ))

            execute_values(cur, "INSERT INTO pubmed (pmid, pm_status, year, ti, ab, pm_data, source_filename, clf_type, clf_score, clf_date, ptyp_rct, is_rct_precise, is_rct_balanced, is_rct_sensitive, indexing_method, score_svm, score_cnn, score_svm_cnn, score_svm_ptyp, score_cnn_ptyp, score_svm_cnn_ptyp, rct_probability, is_human, update_date) VALUES %s ON CONFLICT (pmid) DO UPDATE SET year=EXCLUDED.year, ti=EXCLUDED.ti, ab=EXCLUDED.ab, pm_data=EXCLUDED.pm_data, source_filename=EXCLUDED.source_filename, clf_type=EXCLUDED.clf_type, clf_score=EXCLUDED.clf_score, clf_date=EXCLUDED.clf_date, ptyp_rct=EXCLUDED.ptyp_rct, is_rct_precise=EXCLUDED.is_rct_precise, is_rct_balanced=EXCLUDED.is_rct_balanced, is_rct_sensitive=EXCLUDED.is_rct_sensitive, indexing_method=EXCLUDED.indexing_method, rct_probability=EXCLUDED.rct_probability, is_human=EXCLUDED.is_human, update_date=EXCLUDED.update_date;", include_rows, template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)")


            execute_values(cur, "INSERT INTO pubmed_excludes (pmid, pm_status, year, source_filename, clf_type, clf_score, clf_date, ptyp_rct, is_rct_precise, is_rct_balanced, is_rct_sensitive, indexing_method, score_svm, score_cnn, score_svm_cnn, score_svm_ptyp, score_cnn_ptyp, score_svm_cnn_ptyp, rct_probability, is_human, update_date) VALUES %s ON CONFLICT (pmid) DO UPDATE SET year=EXCLUDED.year, source_filename=EXCLUDED.source_filename, clf_type=EXCLUDED.clf_type, clf_score=EXCLUDED.clf_score, clf_date=EXCLUDED.clf_date, ptyp_rct=EXCLUDED.ptyp_rct, is_rct_precise=EXCLUDED.is_rct_precise, is_rct_balanced=EXCLUDED.is_rct_balanced, is_rct_sensitive=EXCLUDED.is_rct_sensitive, indexing_method=EXCLUDED.indexing_method, rct_probability=EXCLUDED.rct_probability, is_human=EXCLUDED.is_human, update_date=EXCLUDED.update_date;", exclude_rows, template="(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)")

            cur.close()
            dbutil.db.commit()
        if updates:
            dbutil.log_update(update_type='pubmed_update', source_filename=os.path.basename(ftp_fn), source_date=modtimes[os.path.basename(ftp_fn)], download_date=datetime.datetime.now())

    log.info(str(stats))
    dbutil.db.commit()
    if len(stats) == 0:
        log.info("There are no new Pubmed updates for now.")

# def meshify_pico():
#     """
#     update an un-meshed table with mesh picos
#     """


def annotate_rcts(force_refresh=False, limit_to='is_rct_balanced', batch_size=100):
    """
    compute picos links from all PubMed articles
    """

    log.warning("Getting PICO spans via RobotReviewer")

    if limit_to not in ['is_rct_balanced', 'is_rct_sensitive',
                        'is_rct_precise']:
        raise Exception('limit_to not recognised, needs to be '
                        '"is_rct_precise","is_rct_balanced", or '
                        '"is_rct_sensitive"')

    if limit_to == 'is_rct_sensitive':
        log.warning("Getting metadata for all articles with sensitive cutoff.."
                    "May take a long time...")


    cur = dbutil.db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    if force_refresh:
        # deleting doi table from database
        log.info('deleting all PICO data from table...')
        cur.execute('delete from pubmed_annotations;')
        already_done_picos = set()
    else:
        log.info('getting list of all PICOs done so far...')
        cur.execute("SELECT pmid FROM pubmed_annotations;")
        records = cur.fetchall()
        already_done_picos = set((r['pmid'] for r in records))


    log.info('Fetching data to annotate')
    cur.execute("SELECT pmid, ti, ab FROM pubmed WHERE {}=true;".format(limit_to))
    records = cur.fetchall()
    print(f'{len(records)} pubmed articles found')
    print(f'{len(already_done_picos)} already_done_picos')
    log.info('PICO annotation in progress')

    for r in tqdm.tqdm(grouper(records, batch_size), desc='100s articles annotated'):

        r_f = [i for i in r if i['pmid'] not in already_done_picos]

        if r_f:

            annotations = predict(r_f, tasks=['pico_span_bot', 'sample_size_bot', 'bias_ab_bot', 'punchline_bot'], filter_rcts='none')

            for a in annotations:


                sample_size = a.get('sample_size_bot', {}).get('num_randomized')
                if sample_size == 'not found' or int(sample_size) > 1000000:
                    sample_size = None

                cur.execute("INSERT INTO pubmed_annotations (pmid, population, interventions, outcomes, population_mesh, interventions_mesh, outcomes_mesh, population_berts, interventions_berts, outcomes_berts, num_randomized, prob_low_rob, punchline_text, effect) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);",
                    (a['pmid'],
                     json.dumps(a['pico_span_bot']['population']),
                     json.dumps(a['pico_span_bot']['interventions']),
                     json.dumps(a['pico_span_bot']['outcomes']),
                     json.dumps(a['pico_span_bot']['population_mesh']),
                     json.dumps(a['pico_span_bot']['interventions_mesh']),
                     json.dumps(a['pico_span_bot']['outcomes_mesh']),
                     a['pico_span_bot']['population_berts'],
                     a['pico_span_bot']['interventions_berts'],
                     a['pico_span_bot']['outcomes_berts'],
                     sample_size,
                     a['bias_ab_bot']['prob_low_rob'],
                     a['punchline_bot']['punchline_text'],
                     a['punchline_bot']['effect']
                     ))

            dbutil.db.commit()

    update_type = "picospan_full" if force_refresh else "picospan_partial"
    dbutil.log_update(update_type=update_type, source_date=datetime.datetime.now())


def update():
    download_ftp_baseline()
    download_ftp_updates()
    annotate_rcts()
    update_counts()

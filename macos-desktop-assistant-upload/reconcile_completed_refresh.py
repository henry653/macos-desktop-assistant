"""Reconcile an owned duplicate queue against a completed slot, without collection."""
import argparse
import fcntl
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from daily_briefing_dispatcher import read_json, atomic_write_json
from wallpaper_manager import _current_desktop_paths, _store_readback, _verify_desktop_presentation, request_wallpaper_update

ROOT = Path(__file__).resolve().parent

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--completed-run-id', required=True)
    parser.add_argument('--restore-if-needed', action='store_true')
    args = parser.parse_args()
    assert all(re.fullmatch(r'[a-zA-Z0-9_-]+', x) for x in (args.run_id, args.completed_run_id))
    started = datetime.now(ZoneInfo('America/New_York'))
    with (ROOT / 'daily_briefing_dispatch.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        queued = read_json(ROOT / 'daily_run_status.json')
        prior = read_json(ROOT / 'run_attempts' / (args.completed_run_id + '.json'))
        state = read_json(ROOT / 'state.json')
        report = read_json(ROOT / 'daily_briefing.json')
        owned_queue = queued.get('run_id') == args.run_id and queued.get('state') == 'queued'
        owned_recovery = args.restore_if_needed and queued.get('state') == 'failed' and queued.get('last_duplicate_attempt',{}).get('run_id') == args.run_id
        assert owned_queue or owned_recovery, 'Queue owner changed'
        assert prior.get('state') in {'ok', 'degraded_complete'}, 'No completed prior attempt'
        assert queued.get('refresh_slot_key') == prior.get('refresh_slot_key') == state.get('last_completed_refresh_slot_key'), 'Not the same completed slot'
        assert report.get('collection_run_id') == args.completed_run_id, 'Report owner changed'
        target = Path(prior['wallpaper']['target'])
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        assert digest == prior['wallpaper']['image_sha256'], 'Wallpaper content changed'
        current = _current_desktop_paths()
        store = _store_readback(target.as_uri())
        presentation = _verify_desktop_presentation(target)
        manifest = read_json(ROOT / 'wallpaper_hotspots.json')
        overlay = read_json(ROOT / 'wallpaper_hotspot_overlay_status.json')
        desktop = current.get('ok') is True and bool(current.get('paths')) and all(Path(p).resolve() == target.resolve() for p in current['paths']) and presentation.get('verified') is True
        spaces = store.get('all_spaces_verified') is True
        lock_source = store.get('lock_screen_source_verified') is True
        recovery = None
        if args.restore_if_needed and not (desktop and spaces and lock_source):
            recovery = request_wallpaper_update(target, valid_for_date=started.date().isoformat(),
                source='local_renderer',source_generated_at=report['generated_at'])
            desktop = recovery.get('current_desktop_verified') is True
            spaces = recovery.get('all_spaces_verified') is True
            lock_source = recovery.get('lock_screen_source_verified') is True
            presentation = recovery.get('presentation') or presentation
        checked_at = datetime.now(ZoneInfo('America/New_York')).isoformat()
        click = overlay.get('state') == 'active' and overlay.get('hotspot_count') == len(manifest.get('hotspots', [])) and overlay.get('image_sha256') == manifest.get('image_sha256') == digest
        receipt = {'state':'duplicate_skipped', 'run_id':args.run_id, 'duplicate_of':args.completed_run_id,
            'refresh_slot_key':prior['refresh_slot_key'], 'started_at':started.isoformat(), 'finished_at':checked_at,
            'lease_expires_at':None, 'reason':'same_slot_already_completed', 'external_writes':0,
            'applications_opened':0, 'applications_submitted':0, 'report_generated_this_run':False,
            'wallpaper_generated_this_run':False, 'wallpaper_reapplied':recovery is not None,
            'data_report':{'status':'preserved_previous_partial' if prior['state']=='degraded_complete' else 'preserved_previous',
                'identity_verified':True, 'fresh_sources_checked_this_run':False, 'generated_at':report['generated_at']},
            'wallpaper':{'status':'ok' if desktop and spaces and lock_source else 'verification_failed',
                'target':str(target),'image_sha256':digest,'checked_at':checked_at,'current_desktop_verified':desktop,
                'all_spaces_verified':spaces,'lock_screen_source_verified':lock_source,
                'presentation_verified':presentation.get('verified'),'presentation_verified_at':presentation.get('checked_at'),
                'error':presentation.get('error')},
            'click_layer':{'verified':click,'hotspot_count':overlay.get('hotspot_count')},
            'elapsed_seconds':round((datetime.fromisoformat(checked_at)-started).total_seconds(),3)}
        if owned_queue:
            atomic_write_json(ROOT/'run_attempts'/(args.run_id+'-queued.json'),queued)
        atomic_write_json(ROOT/'run_attempts'/(args.run_id+'.json'),receipt)
        prior['last_duplicate_attempt'] = receipt
        prior['last_idempotency_check_at'] = checked_at
        prior['lease_expires_at'] = None
        prior['wallpaper'] = receipt['wallpaper']
        prior['click_layer'] = receipt['click_layer']
        if not (desktop and spaces and lock_source):
            prior['state'] = 'failed'
            prior['error'] = 'existing_wallpaper_verification_failed'
        atomic_write_json(ROOT/'daily_run_status.json',prior)
        print(json.dumps(receipt,ensure_ascii=False))

if __name__ == '__main__':
    main()

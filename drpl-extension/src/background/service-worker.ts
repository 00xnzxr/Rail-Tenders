// ============================================================
// DRPL Extension - Background Service Worker (Manifest V3)
// Orchestrates: message routing, data batching, API sync,
// alarm scheduling, scraping coordination, and deep scraping
// ============================================================

import { uploadTenders, reportStatus, fetchSelectors, uploadDocument, syncScopeProfile } from '../utils/api-client';
import { getSettings, markTendersExtracted, isTenderExtracted, addScrapeSession, updateLatestSession, getStorage, setStorage } from '../utils/storage';
import { TenderData, TenderDataMessage, ExtensionMessage, ScrapeSession, DeepScrapeProgress, DocumentLinkInfo, TenderProgress, ScrapeJobProgress, ScopeProfile } from '../utils/types';
import { splitByScope } from '../utils/scope-matcher';

// --- State (in-memory, but we also persist key metrics to storage) ---
let pendingTenders: TenderData[] = [];
let isUploading = false;
let isDeepScraping = false;
let isDocumentHunting = false;
const BATCH_SIZE = 25; // Upload in chunks of 25
const ALARM_NAME = 'drpl-scheduled-scrape';
const DEEP_SCRAPE_TIMEOUT_MS = 20000; // 20s per detail page
const DEFAULT_DEEP_SCRAPE_DELAY_MS = 2500;
const AUTO_CAPTURE_MAX_PER_SESSION = 10;

// --- Portal URLs ---
const PORTAL_URLS: Record<string, string> = {
  ireps: 'https://www.ireps.gov.in',
  gem: 'https://gem.gov.in',
  tendertiger: 'https://www.tendertiger.com',
  bidassist: 'https://www.bidassist.com',
  tenderdetail: 'https://www.tenderdetail.com',
  tendersinfo: 'https://www.tendersinfo.com',
  projectstoday: 'https://www.projectstoday.com',
};

// --- Message Handler ---

chrome.runtime.onMessage.addListener((message: ExtensionMessage, sender, sendResponse) => {
  console.log('[Ext] Received message:', message.type);

  switch (message.type) {
    case 'TENDER_DATA_EXTRACTED':
      handleTenderData(message as TenderDataMessage, (message as any).manual === true);
      sendResponse({ received: true });
      break;

    case 'START_GUIDED_SCRAPE':
      handleGuidedScrape(message.portal || 'ireps', message.payload);
      sendResponse({ started: true });
      break;

    case 'SCRAPE_CURRENT_TAB':
      handleScrapeCurrentTab(message.payload?.tabId, message.payload?.tenderLimit || message.payload?.pageCount).then(sendResponse);
      return true; // Async response — keep channel open

    case 'START_DEEP_SCRAPE':
      handleDeepScrapeRequest(message.payload?.tenders || []);
      sendResponse({ started: true });
      break;

    case 'DETAIL_PAGE_EXTRACTED':
      // Handled via tab messaging in deep scrape loop, not here
      sendResponse({ received: true });
      break;

    case 'SESSION_EXPIRED':
      handleSessionExpired(message.portal || 'ireps');
      sendResponse({ acknowledged: true });
      break;

    case 'GET_SCRAPE_STATUS':
      getScrapeStatus().then(sendResponse);
      return true; // Async response

    case 'GET_CONFIG':
      getSettings().then(sendResponse);
      return true;

    case 'TRIGGER_SCRAPE':
    case 'EXTRACT_DETAIL':
    case 'RUN_GEM_AUTO_SEARCH':
      // These are handled by content scripts, not the service worker.
      // Don't respond — let the content script handle it.
      break;

    // Phase 7 — scope-driven scraping orchestration
    case 'START_GEM_AUTO_SEARCH':
      handleStartGemAutoSearch().then(sendResponse);
      return true;

    case 'START_IREPS_ELIGIBLE_SCAN':
      handleStartIrepsScan().then(sendResponse);
      return true;

    case 'START_IREPS_AUTO_SEARCH':
      handleStartIrepsAutoSearch().then(sendResponse);
      return true;

    case 'RUN_IREPS_AUTO_SEARCH':
      // Handled by content script, not the service worker.
      break;

    case 'IREPS_AUTO_SEARCH_SUBMITTED':
      handleIrepsAutoSearchSubmitted(message.payload || {});
      sendResponse({ received: true });
      break;

    case 'IREPS_SEARCH_PAGINATION':
      handleIrepsResultsPagination(message.payload || {}).catch((err) =>
        console.error('[Ext] IREPS pagination walk failed:', err),
      );
      sendResponse({ received: true });
      break;

    case 'GEM_AUTO_SEARCH_PROGRESS':
      handleGemAutoSearchProgress(message.payload || {});
      sendResponse({ received: true });
      break;

    case 'GEM_AUTO_SEARCH_COMPLETE':
      handleGemAutoSearchComplete(message.payload || {});
      sendResponse({ received: true });
      break;

    case 'SYNC_SCOPE_PROFILE':
      syncScopeProfile().then((profile) => sendResponse({ profile }));
      return true;

    case 'START_DOCUMENT_HUNT':
      handleStartDocumentHunt().then(sendResponse);
      return true;

    case 'HUNT_DOCS_FOR_TENDER':
      handleHuntDocsForTender(message.payload?.tender).then(sendResponse);
      return true;

    default:
      // Silently ignore unknown messages (may be from content scripts or other extensions)
      break;
  }
});

// --- Tender Data Processing ---

async function handleTenderData(message: TenderDataMessage, isManual: boolean = false) {
  const { tenders, pageType, pageUrl } = message.payload;
  const portal = message.portal || 'unknown';

  console.log(`[Ext] Received ${tenders.length} tenders from ${portal} (manual: ${isManual})`);

  if (tenders.length === 0) {
    console.log('[Ext] No tenders to process');
    return;
  }

  // Create a scrape session (one per user action)
  const session: ScrapeSession = {
    id: `${isManual ? 'manual' : 'auto'}-${portal}-${Date.now()}`,
    portal: portal as any,
    startedAt: new Date().toISOString(),
    completedAt: null,
    tendersFound: tenders.length,
    status: 'running',
  };
  await addScrapeSession(session);

  // For manual scrapes (user clicked "Scrape N"), process ALL N tenders regardless
  // of prior history. Backend dedups via (portal, tender_id) unique constraint.
  let newTenders: TenderData[];
  if (isManual) {
    newTenders = tenders;
    console.log(`[Ext] Manual scrape: processing all ${tenders.length} tenders (skipping local dedup)`);
  } else {
    newTenders = [];
    for (const tender of tenders) {
      const alreadyExtracted = await isTenderExtracted(tender.tenderId);
      if (!alreadyExtracted) newTenders.push(tender);
    }
  }

  if (newTenders.length === 0) {
    console.log('[Ext] All tenders already extracted, skipping');
    await updateLatestSession({
      status: 'completed',
      completedAt: new Date().toISOString(),
      tendersFound: 0,
    });
    return;
  }

  await markTendersExtracted(newTenders.map((t) => t.tenderId));

  await updateLatestSession({ tendersFound: newTenders.length, status: 'running' });

  // ============================================================
  // PER-TENDER ATOMIC PROCESSING
  // Each tender goes through ALL steps before the next starts:
  //   metadata upload → open detail page → extract docs → upload each doc → done
  // ============================================================
  await processPerTenderAtomic(newTenders);
}

/** PHASE 1 — Metadata only. Fast. Each tender: upload metadata. Then ready for Phase 2. */
async function getAutoCaptureContext(): Promise<{ enabled: boolean; profile: ScopeProfile | null }> {
  const store = await chrome.storage.local.get(['extensionFeatures', 'scopeProfile']);
  const enabled = Boolean(store.extensionFeatures?.auto_capture);
  const profile = (store.scopeProfile as ScopeProfile | undefined) || null;
  return { enabled, profile };
}

async function processPerTenderAtomic(newTenders: TenderData[]) {
  const settings = await getSettings();
  await initJobProgress(newTenders);
  console.log(`[Ext] Phase 1 (metadata): ${newTenders.length} tenders`);

  let errorsCount = 0;
  let lastKeepalive = Date.now();

  for (let i = 0; i < newTenders.length; i++) {
    const tender = newTenders[i];
    const tenderStart = Date.now();
    console.log(`[Ext] [${i + 1}/${newTenders.length}] Metadata: ${tender.tenderId}`);

    await updateJobProgress((j) => {
      j.currentTenderIndex = i;
      const tp = j.tenders[i];
      if (tp) {
        tp.status = 'uploading';
        tp.startedAt = new Date().toISOString();
      }
    });

    try {
      const upload = await uploadTenders([tender]);
      if (!upload.success) throw new Error(`Metadata upload failed: ${upload.error}`);

      const elapsed = Date.now() - tenderStart;
      await updateJobProgress((j) => {
        const tp = j.tenders[i];
        if (tp) {
          tp.status = 'completed';
          tp.completedAt = new Date().toISOString();
        }
        j.averageMsPerTender = j.averageMsPerTender === 0
          ? elapsed
          : j.averageMsPerTender * 0.7 + elapsed * 0.3;
      });
      console.log(`[Ext] [${i + 1}/${newTenders.length}] ${tender.tenderId} metadata OK (${(elapsed / 1000).toFixed(1)}s)`);
    } catch (err) {
      const errMsg = (err as Error).message || String(err);
      console.error(`[Ext] [${i + 1}/${newTenders.length}] ${tender.tenderId} ERROR:`, errMsg);
      errorsCount++;
      await updateJobProgress((j) => {
        const tp = j.tenders[i];
        if (tp) {
          tp.status = 'error';
          tp.error = errMsg;
          tp.completedAt = new Date().toISOString();
        }
        j.errorsCount = errorsCount;
      });
    }

    await updateLatestSession({ uploadedCount: i + 1 - errorsCount });

    if (Date.now() - lastKeepalive > 25000) {
      await chrome.storage.local.set({ _keepalive: Date.now() });
      lastKeepalive = Date.now();
    }
  }

  // Persist tenders eligible for document hunting (those with a detailUrl)
  const huntable = newTenders.filter((t) => t.detailUrl);
  await chrome.storage.local.set({ pendingDocumentHunt: huntable });

  // Finalize the scrape session's telemetry now, while it is still the
  // "latest" session — the auto-capture hunt below creates its own
  // 'hunt-<ts>' session which would otherwise become "latest" first and
  // absorb this completion update, leaving the scrape session stuck at
  // status 'running'.
  await updateLatestSession({
    status: 'completed',
    completedAt: new Date().toISOString(),
    uploadedCount: newTenders.length - errorsCount,
  });

  // Piece B — gated auto-capture: for in-scope tenders, run the document hunt
  // automatically (capped, sequential). Overflow + out-of-scope stay in the
  // pending queue for the manual "Hunt Documents" button.
  const { enabled: autoCapture, profile: scopeProfile } = await getAutoCaptureContext();
  if (autoCapture && scopeProfile) {
    const { capture, overflow, skipped } = splitByScope(
      huntable, scopeProfile, AUTO_CAPTURE_MAX_PER_SESSION,
    );

    if (capture.length > 0 && isDocumentHunting) {
      // A hunt (manual or otherwise) is already running — don't start a
      // second concurrent one. Leave the full huntable set (capture +
      // overflow + skipped) available for the manual "Hunt Documents" button.
      console.log('[Ext] Auto-capture skipped: a hunt is already in progress');
      await chrome.storage.local.set({ pendingDocumentHunt: [...capture, ...overflow, ...skipped] });
    } else {
      // Remainder that the manual button should still be able to hunt.
      const remainder = [...overflow, ...skipped];
      await chrome.storage.local.set({ pendingDocumentHunt: remainder });

      if (capture.length > 0) {
        console.log(
          `[Ext] Auto-capture: ${capture.length} in-scope` +
          (overflow.length ? `, ${overflow.length} over cap left for manual hunt` : ''),
        );
        isDocumentHunting = true;
        try {
          const res = await processDocumentHunt(capture, { clearQueue: false, forceDownload: true });
          if (settings.notificationsEnabled) {
            chrome.notifications.create({
              type: 'basic',
              iconUrl: 'icons/icon-48.png',
              title: 'Auto-capture complete',
              message:
                `${res.docsUploaded} docs auto-captured for ${capture.length} in-scope tender(s).` +
                (overflow.length ? ` ${overflow.length} over cap left for manual hunt.` : ''),
            });
          }
        } finally {
          isDocumentHunting = false;
        }
      } else {
        console.log('[Ext] Auto-capture: no in-scope tenders this session.');
      }
    }
  }

  await updateJobProgress((j) => {
    j.status = 'completed';
    j.currentTenderIndex = j.totalTenders;
  });

  console.log(`[Ext] Phase 1 done: ${newTenders.length - errorsCount}/${newTenders.length} metadata uploaded. ${huntable.length} ready for document hunting.`);

  if (settings.notificationsEnabled) {
    chrome.notifications.create({
      type: 'basic',
      iconUrl: 'icons/icon-48.png',
      title: 'Scrape complete',
      message: `${newTenders.length - errorsCount}/${newTenders.length} tenders scraped. Click "Hunt Documents" to fetch PDFs.`,
    });
  }
}

/** PHASE 2 — User-triggered. Open each tender's detail page, extract docs, upload PDFs. */
async function processDocumentHunt(
  tenders: TenderData[],
  opts: { clearQueue?: boolean; forceDownload?: boolean } = {},
): Promise<{ docsUploaded: number; errors: number }> {
  const { clearQueue = true, forceDownload = false } = opts;
  const settings = await getSettings();
  // Re-init job progress in "hunt mode"
  await initJobProgress(tenders);
  await updateJobProgress((j) => { j.status = 'running'; });

  console.log(`[Ext] Phase 2 (document hunt): ${tenders.length} tenders`);

  let totalDocsUploaded = 0;
  let errorsCount = 0;
  let lastKeepalive = Date.now();

  await addScrapeSession({
    id: `hunt-${Date.now()}`,
    portal: (tenders[0]?.portal || 'ireps') as any,
    startedAt: new Date().toISOString(),
    completedAt: null,
    tendersFound: tenders.length,
    status: 'deep_scraping',
  });

  for (let i = 0; i < tenders.length; i++) {
    const tender = tenders[i];
    const tenderStart = Date.now();
    console.log(`[Ext] [${i + 1}/${tenders.length}] Hunting docs for ${tender.tenderId}`);

    await updateJobProgress((j) => {
      j.currentTenderIndex = i;
      const tp = j.tenders[i];
      if (tp) {
        tp.status = 'extracting';
        tp.startedAt = new Date().toISOString();
      }
    });

    let tenderDocsUploaded = 0;
    try {
      // Open viewNIT detail page in background tab and extract documents
      let docs: DocumentLinkInfo[] = tender.classifiedDocuments || [];
      let enrichedData: Partial<TenderData> | null = null;

      if (tender.detailUrl) {
        enrichedData = await extractDetailFromTab(tender.detailUrl);
        if (enrichedData?.classifiedDocuments?.length) {
          docs = enrichedData.classifiedDocuments;
        }
      }

      await updateJobProgress((j) => {
        const tp = j.tenders[i];
        if (tp) {
          tp.status = 'downloading';
          tp.documentsTotal = docs.length;
        }
      });

      if (forceDownload || settings.downloadDocumentsEnabled) {
        for (let d = 0; d < docs.length; d++) {
          const doc = docs[d];
          try {
            await downloadAndUploadDocument(tender.tenderId, doc, tender.portal);
            tenderDocsUploaded++;
            totalDocsUploaded++;
          } catch (err) {
            console.warn(`[Ext] Doc upload failed (${tender.tenderId}): ${doc.url}`, err);
          }
          await updateJobProgress((j) => {
            const tp = j.tenders[i];
            if (tp) {
              tp.documentsCompleted = d + 1;
              tp.documentsUploaded = tenderDocsUploaded;
            }
            j.totalDocsUploaded = totalDocsUploaded;
          });
        }
      }

      // Re-upload enriched metadata so the backend gets the full detail-page data
      if (enrichedData) {
        const enrichedTender: TenderData = { ...tender, ...enrichedData, isDetailExtracted: true };
        await uploadTenders([enrichedTender]).catch((e) =>
          console.warn(`[Ext] Enriched metadata upload failed: ${e}`),
        );
      }

      const elapsed = Date.now() - tenderStart;
      await updateJobProgress((j) => {
        const tp = j.tenders[i];
        if (tp) {
          tp.status = 'completed';
          tp.completedAt = new Date().toISOString();
        }
        j.averageMsPerTender = j.averageMsPerTender === 0
          ? elapsed
          : j.averageMsPerTender * 0.7 + elapsed * 0.3;
      });
      console.log(`[Ext] [${i + 1}/${tenders.length}] ${tender.tenderId}: ${tenderDocsUploaded}/${docs.length} docs in ${(elapsed / 1000).toFixed(1)}s`);
    } catch (err) {
      const errMsg = (err as Error).message || String(err);
      console.error(`[Ext] Hunt error for ${tender.tenderId}:`, errMsg);
      errorsCount++;
      await updateJobProgress((j) => {
        const tp = j.tenders[i];
        if (tp) {
          tp.status = 'error';
          tp.error = errMsg;
          tp.completedAt = new Date().toISOString();
        }
        j.errorsCount = errorsCount;
      });
    }

    await updateLatestSession({
      deepScrapeCompleted: i + 1,
      documentsDownloaded: totalDocsUploaded,
    });

    if (Date.now() - lastKeepalive > 25000) {
      await chrome.storage.local.set({ _keepalive: Date.now() });
      lastKeepalive = Date.now();
    }
    if (i < tenders.length - 1) await sleep(500);
  }

  await updateJobProgress((j) => {
    j.status = 'completed';
    j.currentTenderIndex = j.totalTenders;
  });

  await updateLatestSession({
    status: 'completed',
    completedAt: new Date().toISOString(),
    deepScrapeCompleted: tenders.length,
    documentsDownloaded: totalDocsUploaded,
  });

  // Clear the pending hunt queue only for the manual/full run. Auto-capture
  // passes clearQueue:false and manages the remainder itself.
  if (clearQueue) {
    await chrome.storage.local.remove('pendingDocumentHunt');
  }

  console.log(`[Ext] Phase 2 done: ${totalDocsUploaded} docs uploaded across ${tenders.length} tenders, ${errorsCount} errors`);

  if (settings.notificationsEnabled) {
    chrome.notifications.create({
      type: 'basic',
      iconUrl: 'icons/icon-48.png',
      title: 'Document hunt complete',
      message: `${totalDocsUploaded} documents uploaded across ${tenders.length} tenders.`,
    });
  }

  return { docsUploaded: totalDocsUploaded, errors: errorsCount };
}

// --- Chunked Upload ---

async function uploadAllPending() {
  if (isUploading) return; // Already running
  isUploading = true;

  let totalUploaded = 0;
  let lastError = '';
  let lastKeepalive = Date.now();

  while (pendingTenders.length > 0) {
    // Take a chunk
    const chunk = pendingTenders.splice(0, BATCH_SIZE);
    await persistPendingCount();

    console.log(`[Ext] Uploading chunk of ${chunk.length} (${pendingTenders.length} remaining)`);

    // Keepalive to prevent service worker idle kill
    if (Date.now() - lastKeepalive > 25000) {
      await chrome.storage.local.set({ _keepalive: Date.now() });
      lastKeepalive = Date.now();
    }

    const result = await uploadTenders(chunk);

    if (result.success) {
      totalUploaded += chunk.length;
      console.log(`[Ext] Chunk uploaded: ${result.data?.new} new, ${result.data?.duplicates} duplicates`);

      // Update session progress
      await updateLatestSession({
        status: pendingTenders.length > 0 ? 'uploading' : 'completed',
        uploadedCount: totalUploaded,
      });
    } else {
      console.warn('[Ext] Chunk upload failed, retrying once:', result.error);
      await sleep(2000);
      const retry = await uploadTenders(chunk);
      if (retry.success) {
        totalUploaded += chunk.length;
        console.log(`[Ext] Retry succeeded: ${retry.data?.new} new, ${retry.data?.duplicates} duplicates`);
      } else {
        console.error('[Ext] Chunk upload failed after retry, skipping:', retry.error);
        lastError = retry.error || 'Upload failed';
        // Skip this chunk and continue — don't block the entire pipeline
      }
    }
  }

  if (pendingTenders.length === 0 && totalUploaded > 0) {
    // All done
    await updateLatestSession({
      status: 'completed',
      completedAt: new Date().toISOString(),
    });
    await setStorage('lastSyncTimestamp', new Date().toISOString());
    await chrome.storage.local.remove(['lastUploadError', 'lastUploadErrorAt']);

    // Notification
    const settings = await getSettings();
    if (settings.notificationsEnabled) {
      chrome.notifications.create({
        type: 'basic',
        iconUrl: 'icons/icon-48.png',
        title: 'Sync complete',
        message: `${totalUploaded} items uploaded to your workspace.`,
      });
    }
  }

  isUploading = false;
}

async function persistPendingCount() {
  await chrome.storage.local.set({ pendingUploadCount: pendingTenders.length });
}

// ============================================================
// --- Deep Scrape Orchestration ---
// Queue-based, parallel (3 tabs), with keepalive
// ============================================================

// Sequential processing: 1 tender at a time, fully complete before next.
// Quality over quantity — ~30s/tender but 100% reliable.
const CONCURRENT_TABS = 1;
let deepScrapeQueue: TenderData[] = [];

// --- Per-tender progress tracking ---

async function initJobProgress(tenders: TenderData[]): Promise<ScrapeJobProgress> {
  const job: ScrapeJobProgress = {
    jobId: `job-${Date.now()}`,
    startedAt: new Date().toISOString(),
    totalTenders: tenders.length,
    currentTenderIndex: 0,
    tenders: tenders.map((t) => ({
      tenderId: t.tenderId,
      title: t.title || t.tenderId,
      status: 'pending',
      documentsTotal: t.classifiedDocuments?.length || 0,
      documentsCompleted: 0,
      documentsUploaded: 0,
      startedAt: null,
      completedAt: null,
    })),
    averageMsPerTender: 0,
    status: 'running',
    totalDocsUploaded: 0,
    errorsCount: 0,
  };
  await chrome.storage.local.set({ scrapeJobProgress: job });
  return job;
}

async function updateJobProgress(updates: (job: ScrapeJobProgress) => void): Promise<void> {
  const stored = await chrome.storage.local.get('scrapeJobProgress');
  const job: ScrapeJobProgress | undefined = stored.scrapeJobProgress;
  if (!job) return;
  updates(job);
  await chrome.storage.local.set({ scrapeJobProgress: job });
}

async function handleDeepScrapeRequest(tenders: TenderData[]) {
  const toScrape = tenders.filter((t) => t.detailUrl && !t.isDetailExtracted);
  if (toScrape.length === 0) return;

  if (isDeepScraping) {
    deepScrapeQueue.push(...toScrape);
    console.log(`[Ext] Queued ${toScrape.length} tenders for deep scrape (${deepScrapeQueue.length} in queue)`);
    return;
  }

  isDeepScraping = true;
  await processDeepScrapeBatch(toScrape);

  while (deepScrapeQueue.length > 0) {
    const batch = deepScrapeQueue.splice(0, deepScrapeQueue.length);
    const filtered = batch.filter((t) => t.detailUrl && !t.isDetailExtracted);
    if (filtered.length > 0) {
      await processDeepScrapeBatch(filtered);
    }
  }

  isDeepScraping = false;
}

async function processDeepScrapeBatch(tendersToScrape: TenderData[]) {
  const settings = await getSettings();
  let lastKeepalive = Date.now();

  console.log(`[Ext] Deep scrape: ${tendersToScrape.length} tenders SEQUENTIALLY`);

  // Initialize per-tender progress
  const job = await initJobProgress(tendersToScrape);

  await updateLatestSession({
    status: 'deep_scraping',
    deepScrapeTotal: tendersToScrape.length,
    deepScrapeCompleted: 0,
  });

  const enrichedTenders: TenderData[] = [];
  let totalDocsUploaded = 0;
  let errorsCount = 0;

  for (let i = 0; i < tendersToScrape.length; i++) {
    const tender = tendersToScrape[i];
    const tenderStart = Date.now();
    console.log(`[Ext] [${i + 1}/${tendersToScrape.length}] Processing tender: ${tender.tenderId}`);

    // Mark this tender as in-progress
    await updateJobProgress((job) => {
      job.currentTenderIndex = i;
      const tp = job.tenders[i];
      if (tp) {
        tp.status = 'extracting';
        tp.startedAt = new Date().toISOString();
      }
    });

    try {
      // 1. Extract from detail page (viewNIT popup)
      const enrichedData = await extractDetailFromTab(tender.detailUrl!);
      const docs = enrichedData?.classifiedDocuments || tender.classifiedDocuments || [];

      // 2. Update doc count for this tender
      await updateJobProgress((job) => {
        const tp = job.tenders[i];
        if (tp) {
          tp.status = 'downloading';
          tp.documentsTotal = docs.length;
        }
      });

      const enrichedTender: TenderData = enrichedData
        ? { ...tender, ...enrichedData, isDetailExtracted: true }
        : { ...tender, isDetailExtracted: false };

      // 3. Download + upload each document SEQUENTIALLY
      if (settings.downloadDocumentsEnabled) {
        for (let d = 0; d < docs.length; d++) {
          const doc = docs[d];
          try {
            await downloadAndUploadDocument(tender.tenderId, doc, tender.portal);
            totalDocsUploaded++;
            await updateJobProgress((job) => {
              const tp = job.tenders[i];
              if (tp) {
                tp.documentsCompleted = d + 1;
                tp.documentsUploaded = d + 1;
              }
              job.totalDocsUploaded = totalDocsUploaded;
            });
          } catch (err) {
            console.warn(`[Ext] Doc upload failed (${tender.tenderId}): ${doc.url}`, err);
            await updateJobProgress((job) => {
              const tp = job.tenders[i];
              if (tp) tp.documentsCompleted = d + 1;
            });
          }
        }
      }

      enrichedTenders.push(enrichedTender);

      // 4. Mark this tender as complete + update ETA
      const elapsed = Date.now() - tenderStart;
      await updateJobProgress((job) => {
        const tp = job.tenders[i];
        if (tp) {
          tp.status = 'completed';
          tp.completedAt = new Date().toISOString();
        }
        // Weighted rolling average for ETA
        job.averageMsPerTender = job.averageMsPerTender === 0
          ? elapsed
          : job.averageMsPerTender * 0.7 + elapsed * 0.3;
      });
    } catch (err) {
      const errMsg = (err as Error).message || 'Unknown error';
      console.error(`[Ext] Error processing tender ${tender.tenderId}:`, err);
      errorsCount++;
      await updateJobProgress((job) => {
        const tp = job.tenders[i];
        if (tp) {
          tp.status = 'error';
          tp.error = errMsg;
          tp.completedAt = new Date().toISOString();
        }
        job.errorsCount = errorsCount;
      });
      enrichedTenders.push({ ...tender, isDetailExtracted: false });
    }

    await updateLatestSession({
      deepScrapeCompleted: i + 1,
      documentsDownloaded: totalDocsUploaded,
    });

    if (Date.now() - lastKeepalive > 25000) {
      await chrome.storage.local.set({ _keepalive: Date.now() });
      lastKeepalive = Date.now();
    }

    // Brief pause between tenders to be polite
    if (i < tendersToScrape.length - 1) await sleep(500);
  }

  // Upload enriched tenders back to backend
  const enrichedToUpload = enrichedTenders.filter((t) => t.isDetailExtracted);
  if (enrichedToUpload.length > 0) {
    console.log(`[Ext] Uploading ${enrichedToUpload.length} enriched tenders`);
    pendingTenders.push(...enrichedToUpload);
    await persistPendingCount();
    await uploadAllPending();
  }

  // Mark job as complete
  await updateJobProgress((job) => {
    job.status = 'completed';
    job.currentTenderIndex = job.totalTenders;
  });

  await updateLatestSession({
    status: 'completed',
    completedAt: new Date().toISOString(),
    deepScrapeCompleted: tendersToScrape.length,
    documentsDownloaded: totalDocsUploaded,
  });

  await chrome.storage.local.remove('deepScrapeState');

  if (settings.notificationsEnabled) {
    chrome.notifications.create({
      type: 'basic',
      iconUrl: 'icons/icon-48.png',
      title: 'Scrape complete',
      message: `${enrichedToUpload.length}/${tendersToScrape.length} tenders processed. ${totalDocsUploaded} documents uploaded. ${errorsCount} errors.`,
    });
  }
}

/**
 * Open a detail page URL in a background tab, send EXTRACT_DETAIL to the
 * content script, wait for the response, then close the tab.
 */
async function extractDetailFromTab(url: string): Promise<Partial<TenderData> | null> {
  return new Promise((resolve) => {
    let tabId: number | null = null;
    let resolved = false;

    const timeoutId = setTimeout(() => {
      if (!resolved) {
        resolved = true;
        console.warn(`[Ext] Detail extraction timed out for ${url}`);
        if (tabId) chrome.tabs.remove(tabId).catch(() => {});
        resolve(null);
      }
    }, DEEP_SCRAPE_TIMEOUT_MS);

    chrome.tabs.create({ url, active: false }, (tab) => {
      if (!tab?.id) {
        clearTimeout(timeoutId);
        resolve(null);
        return;
      }
      tabId = tab.id;

      // Wait for the page to load, then send EXTRACT_DETAIL
      const onUpdated = (updatedTabId: number, changeInfo: chrome.tabs.TabChangeInfo) => {
        if (updatedTabId !== tabId || changeInfo.status !== 'complete') return;
        chrome.tabs.onUpdated.removeListener(onUpdated);

        // Small delay to let content script initialize
        setTimeout(() => {
          if (resolved) return;

          chrome.tabs.sendMessage(tabId!, { type: 'EXTRACT_DETAIL' }, (response) => {
            if (resolved) return;
            resolved = true;
            clearTimeout(timeoutId);

            if (chrome.runtime.lastError) {
              console.warn(`[Ext] Content script not reachable on detail page: ${chrome.runtime.lastError.message}`);
              chrome.tabs.remove(tabId!).catch(() => {});
              resolve(null);
              return;
            }

            chrome.tabs.remove(tabId!).catch(() => {});

            if (response?.data) {
              resolve(response.data as Partial<TenderData>);
            } else {
              resolve(null);
            }
          });
        }, 1500); // Wait 1.5s after load for content script to be ready
      };

      chrome.tabs.onUpdated.addListener(onUpdated);
    });
  });
}

// --- Document Download ---

async function downloadAndUploadDocument(
  tenderId: string,
  doc: DocumentLinkInfo,
  portal?: string,
): Promise<void> {
  // Skip URLs that are clearly HTML pages, not downloadable documents
  const urlLower = doc.url.toLowerCase();
  if (urlLower.includes('advancedsearch.do') || urlLower.includes('pageno=')) {
    console.log(`[Ext] Skipping non-document URL: ${doc.url}`);
    return;
  }

  console.log(`[Ext] Downloading document: ${doc.label} from ${doc.url}`);

  const response = await fetch(doc.url);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} downloading ${doc.url}`);
  }

  // Check Content-Type — skip HTML pages masquerading as documents
  const contentType = (response.headers.get('content-type') || '').toLowerCase();
  if (contentType.includes('text/html') && !contentType.includes('pdf')) {
    console.log(`[Ext] Skipping HTML response (${contentType}): ${doc.url}`);
    return;
  }

  const blob = await response.blob();

  // Skip very large files (>10MB)
  if (blob.size > 10 * 1024 * 1024) {
    console.warn(`[Ext] Skipping large file (${(blob.size / 1024 / 1024).toFixed(1)}MB): ${doc.url}`);
    return;
  }

  // Extract filename from URL or use label
  let fileName = '';
  try {
    fileName = new URL(doc.url).pathname.split('/').pop() || '';
  } catch {
    // ignore URL parse errors
  }
  if (!fileName || !/\.[a-z0-9]{1,8}$/i.test(fileName)) {
    // Synthesise a stable filename when the URL has no usable basename.
    // Use the label slug so re-uploads of the same doc collide and dedup.
    const slug = (doc.label || doc.type || 'document')
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 60) || 'document';
    fileName = `${slug}_${tenderId}.pdf`;
  }

  // Pass portal so the backend can resolve a non-numeric portal tender id
  // (e.g. "L9265359A") to a real Tender row.
  await uploadDocument(tenderId, fileName, blob, doc.type, portal, doc.url);
  console.log(`[Ext] Uploaded document: ${fileName} (${(blob.size / 1024).toFixed(0)}KB)`);
}

// --- Guided Scraping (legacy) ---

async function handleGuidedScrape(portal: string, payload?: any) {
  const session: ScrapeSession = {
    id: `scrape-${Date.now()}`,
    portal: portal as any,
    startedAt: new Date().toISOString(),
    completedAt: null,
    tendersFound: 0,
    status: 'running',
  };

  await addScrapeSession(session);

  const url = PORTAL_URLS[portal];
  if (!url) {
    await updateLatestSession({ status: 'error', error: 'Unknown portal' });
    return;
  }

  chrome.tabs.create({ url, active: true }, (tab) => {
    console.log(`[Ext] Opened guided scrape tab for ${portal}:`, tab?.id);
  });
}

// --- Scrape Current Tab ---

async function handleScrapeCurrentTab(tabId?: number, tenderLimit?: number): Promise<object> {
  if (!tabId) return { error: 'No tab ID provided' };

  const limit = tenderLimit || 10;
  console.log(`[Ext] Triggering scrape on tab ${tabId}, limit: ${limit} tenders`);

  return new Promise((resolve) => {
    chrome.tabs.sendMessage(
      tabId,
      { type: 'TRIGGER_SCRAPE', payload: { tenderLimit: limit } },
      (response) => {
        if (chrome.runtime.lastError) {
          const msg = chrome.runtime.lastError.message || 'Unknown error';
          console.warn('[Ext] Could not reach content script:', msg);
          // Content script not injected — likely extension needs reload or unsupported page
          if (msg.includes('Receiving end does not exist')) {
            resolve({ error: 'Content script not found. Please reload the extension (chrome://extensions) then refresh this page.' });
          } else {
            resolve({ error: msg });
          }
        } else {
          console.log('[Ext] Content script acknowledged scrape trigger:', response);
          resolve({ started: true, extracted: response?.extracted ?? 0 });
        }
      }
    );
  });
}

// --- Session Management ---

function handleSessionExpired(portal: string) {
  console.log(`[Ext] Session expired on ${portal}`);

  chrome.notifications.create({
    type: 'basic',
    iconUrl: 'icons/icon-48.png',
    title: 'Session expired',
    message: 'Your portal session has expired. Please log in again to continue syncing.',
  });
}

// --- Scheduled Scraping (Alarms) ---

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === ALARM_NAME) {
    console.log('[Ext] Scheduled scrape alarm triggered');

    const settings = await getSettings();
    if (settings.notificationsEnabled) {
      chrome.notifications.create({
        type: 'basic',
        iconUrl: 'icons/icon-48.png',
        title: 'Time to sync',
        message: 'Open a supported portal and run a sync to pull the latest items.',
      });
    }
  }
});

// Setup alarm on install/startup
chrome.runtime.onInstalled.addListener(async () => {
  const settings = await getSettings();
  setupScheduledAlarm(settings.scrapeIntervalMinutes);
  console.log('[Ext] Extension installed, alarm set.');

  // Check for interrupted deep scrapes
  resumeInterruptedDeepScrape();
});

chrome.runtime.onStartup.addListener(async () => {
  const settings = await getSettings();
  setupScheduledAlarm(settings.scrapeIntervalMinutes);

  // Check for interrupted deep scrapes
  resumeInterruptedDeepScrape();
});

function setupScheduledAlarm(intervalMinutes: number) {
  chrome.alarms.create(ALARM_NAME, {
    delayInMinutes: intervalMinutes,
    periodInMinutes: intervalMinutes,
  });
}

// --- Deep Scrape Resilience ---

async function resumeInterruptedDeepScrape() {
  const stored = await chrome.storage.local.get('deepScrapeState');
  if (!stored.deepScrapeState) return;

  const { tenders, currentIndex } = stored.deepScrapeState;
  if (currentIndex < tenders.length) {
    console.log(`[Ext] Resuming interrupted deep scrape at ${currentIndex}/${tenders.length}`);
    const remaining = tenders.slice(currentIndex);
    handleDeepScrapeRequest(remaining);
  } else {
    await chrome.storage.local.remove('deepScrapeState');
  }
}

// --- Status ---

async function getScrapeStatus() {
  const history = (await getStorage('scrapeHistory')) || [];
  const lastSync = await getStorage('lastSyncTimestamp');
  const pendingCount = (await chrome.storage.local.get('pendingUploadCount')).pendingUploadCount || 0;
  const deepProgress = (await chrome.storage.local.get('deepScrapeProgress')).deepScrapeProgress || null;
  return {
    pendingCount: pendingTenders.length || pendingCount,
    recentSessions: history.slice(-5),
    lastSync,
    deepScrapeProgress: deepProgress,
    isDeepScraping,
  };
}

// --- Utilities ---

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// ============================================================
// Phase 7 — Scope-driven scraping orchestration
// ============================================================

const GEM_ADVANCE_SEARCH_URL = 'https://bidnext.gem.gov.in/bidnext/#WORKSPACE_ID=ADVANCE_SEARCH_WS';
const IREPS_TENDER_LISTING_URL = 'https://www.ireps.gov.in/epsn/web/login/searchTender';
const IREPS_ADVANCED_SEARCH_URL = 'https://www.ireps.gov.in/epsn/search/advancedSearch.do';

/**
 * Open (or focus) the GeM (bidnext) advance-search page, sync the latest
 * scope profile from the backend, then ask the gem-search-driver content
 * script to iterate the configured departments (or fall back to a single
 * "All / Next 6 Months" pass when nothing is configured).
 */
async function handleStartGemAutoSearch(): Promise<object> {
  const profile = await syncScopeProfile();
  if (!profile) {
    return { error: 'Could not load scope profile from backend.' };
  }
  // We no longer require keyword_groups — the bidnext driver can run a single
  // "All / Next 6 Months" pass when both scope groups and historical departments
  // are empty. That gives a brand-new tenant something useful on day one.

  const tab = await openOrFocusTab(GEM_ADVANCE_SEARCH_URL);
  if (!tab?.id) return { error: 'Failed to open the search tab.' };

  // The content script may still be initialising (SPA hydration, or the tab
  // just got navigated). Retry the dispatch a few times before giving up so
  // a single "Receiving end does not exist" doesn't kill the run.
  const response = await sendToTabWithRetry(
    tab.id,
    { type: 'RUN_GEM_AUTO_SEARCH', payload: { profile } },
    { attempts: 8, initialDelayMs: 1500, betweenDelayMs: 1500 },
  );
  if (response.error) {
    return {
      error:
        `Could not reach the search driver: ${response.error}. ` +
        `Reload the extension at chrome://extensions, then close and reopen the bidnext tab so the latest content script attaches.`,
    };
  }
  return { started: true, ...(response.value || {}) };
}

/** Send a chrome message to a tab, retrying on "Receiving end does not exist"
 * which happens transiently while a content script is hydrating after a
 * navigation. Returns either {value} on success or {error: string} on giving up. */
async function sendToTabWithRetry(
  tabId: number,
  message: unknown,
  opts: { attempts: number; initialDelayMs: number; betweenDelayMs: number },
): Promise<{ value?: any; error?: string }> {
  await sleep(opts.initialDelayMs);
  let lastErr = 'unknown error';
  for (let i = 0; i < opts.attempts; i++) {
    const result = await new Promise<{ value?: any; error?: string }>((resolve) => {
      try {
        chrome.tabs.sendMessage(tabId, message, (response) => {
          if (chrome.runtime.lastError) {
            resolve({ error: chrome.runtime.lastError.message || 'sendMessage failed' });
          } else {
            resolve({ value: response });
          }
        });
      } catch (err) {
        resolve({ error: (err as Error).message });
      }
    });
    if (!result.error) return result;
    lastErr = result.error;
    if (i < opts.attempts - 1) await sleep(opts.betweenDelayMs);
  }
  return { error: lastErr };
}

async function handleStartIrepsScan(): Promise<object> {
  await chrome.storage.local.set({ irepsBlueTickGating: true });
  const tab = await openOrFocusTab(IREPS_TENDER_LISTING_URL);
  if (!tab?.id) return { error: 'Failed to open the listing tab.' };
  await sleep(2500);
  return new Promise((resolve) => {
    chrome.tabs.sendMessage(tab.id!, { type: 'TRIGGER_SCRAPE' }, (response) => {
      if (chrome.runtime.lastError) {
        resolve({ error: `Could not reach the listing scanner: ${chrome.runtime.lastError.message}` });
      } else {
        resolve({ started: true, extracted: response?.extracted ?? 0 });
      }
    });
  });
}

async function handleGemAutoSearchProgress(payload: any): Promise<void> {
  // Persist per-keyword stats so the popup KeywordPanel can render them.
  const { keyword, hits, pages, errors } = payload || {};
  if (!keyword) return;
  const existing = (await chrome.storage.local.get('gemAutoSearchStats')).gemAutoSearchStats || {};
  existing[keyword] = {
    keyword,
    hits: hits ?? 0,
    pages: pages ?? 0,
    errors: errors ?? 0,
    lastRunAt: new Date().toISOString(),
  };
  await chrome.storage.local.set({ gemAutoSearchStats: existing });
}

async function handleGemAutoSearchComplete(payload: any): Promise<void> {
  console.log('[Ext] GeM auto-search complete', payload);
  const settings = await getSettings();
  if (settings.notificationsEnabled) {
    chrome.notifications.create({
      type: 'basic',
      iconUrl: 'icons/icon-48.png',
      title: 'Auto-search complete',
      message: `${payload?.totalHits ?? 0} bids across ${payload?.keywordsRun ?? 0} keywords.`,
    });
  }
}

async function openOrFocusTab(url: string): Promise<chrome.tabs.Tab | null> {
  // Match by hostname + path prefix so existing tabs are reused.
  const target = new URL(url);
  return new Promise((resolve) => {
    chrome.tabs.query({}, (tabs) => {
      const match = tabs.find((t) => {
        try {
          if (!t.url) return false;
          const u = new URL(t.url);
          return u.hostname === target.hostname && u.pathname.startsWith(target.pathname);
        } catch {
          return false;
        }
      });
      if (match?.id) {
        chrome.tabs.update(match.id, { active: true }, (tab) => resolve(tab || null));
      } else {
        chrome.tabs.create({ url, active: true }, (tab) => resolve(tab || null));
      }
    });
  });
}

// ============================================================
// IREPS search results pagination walking
// ============================================================

let isPaginationWalking = false;

async function handleIrepsResultsPagination(pagination: {
  totalCount: number;
  currentPage: number;
  pageUrls: string[];
}): Promise<void> {
  if (isPaginationWalking) {
    console.log('[Ext] IREPS pagination walk already in progress, skipping');
    return;
  }

  const { totalCount, pageUrls } = pagination;
  if (!pageUrls || pageUrls.length === 0) {
    console.log('[Ext] No pagination URLs provided, skipping walk');
    return;
  }

  console.log(`[Ext] IREPS pagination: ${totalCount} results, walking ${pageUrls.length} more pages`);
  isPaginationWalking = true;
  let lastKeepalive = Date.now();

  const settings = await getSettings();
  if (settings.notificationsEnabled) {
    chrome.notifications.create({
      type: 'basic',
      iconUrl: 'icons/icon-48.png',
      title: 'IREPS scrape in progress',
      message: `Extracting ${totalCount} results across ${pageUrls.length + 1} pages...`,
    });
  }

  let pagesWalked = 0;

  try {
    for (let i = 0; i < pageUrls.length; i++) {
      const pageUrl = pageUrls[i];
      console.log(`[Ext] Opening results page ${i + 2}/${pageUrls.length + 1}: ${pageUrl.slice(0, 80)}...`);

      await scrapeFromBackgroundTab(pageUrl);
      pagesWalked++;

      // Keepalive + progress
      if (Date.now() - lastKeepalive > 25000) {
        await chrome.storage.local.set({ _keepalive: Date.now() });
        lastKeepalive = Date.now();
      }
      await chrome.storage.local.set({
        irepsPaginationProgress: {
          currentPage: i + 2,
          totalPages: pageUrls.length + 1,
          pagesWalked,
          updatedAt: new Date().toISOString(),
        },
      });

      if (i < pageUrls.length - 1) await sleep(2000);
    }
  } finally {
    isPaginationWalking = false;
    await chrome.storage.local.remove('irepsPaginationProgress');
  }

  console.log(`[Ext] IREPS pagination walk complete: ${pagesWalked} pages walked`);

  if (settings.notificationsEnabled) {
    chrome.notifications.create({
      type: 'basic',
      iconUrl: 'icons/icon-48.png',
      title: 'IREPS scrape complete',
      message: `Extracted tenders from ${pagesWalked + 1} pages. Deep scrape will follow for detail pages.`,
    });
  }
}

/**
 * Open a results page in a background tab, trigger the content script to
 * extract tenders, wait for it, then close the tab. Returns the count
 * of tenders extracted (they're sent via TENDER_DATA_EXTRACTED separately).
 */
async function scrapeFromBackgroundTab(url: string): Promise<number> {
  return new Promise((resolve) => {
    let tabId: number | null = null;
    let resolved = false;

    const timeoutId = setTimeout(() => {
      if (!resolved) {
        resolved = true;
        console.warn(`[Ext] Scrape timed out for ${url}`);
        if (tabId) chrome.tabs.remove(tabId).catch(() => {});
        resolve(0);
      }
    }, 30000); // 30s timeout per results page

    chrome.tabs.create({ url, active: false }, (tab) => {
      if (!tab?.id) {
        clearTimeout(timeoutId);
        resolve(0);
        return;
      }
      tabId = tab.id;

      const onUpdated = (updatedTabId: number, changeInfo: chrome.tabs.TabChangeInfo) => {
        if (updatedTabId !== tabId || changeInfo.status !== 'complete') return;
        chrome.tabs.onUpdated.removeListener(onUpdated);

        // Wait for the content script to auto-extract (it fires on document_idle).
        // The content script sends TENDER_DATA_EXTRACTED to us directly.
        // We just need to wait a reasonable time then close the tab.
        setTimeout(() => {
          if (resolved) return;
          resolved = true;
          clearTimeout(timeoutId);
          chrome.tabs.remove(tabId!).catch(() => {});
          // We don't know the exact count here since extraction is async,
          // but the tenders arrive via TENDER_DATA_EXTRACTED handler.
          resolve(1); // Signal success
        }, 5000); // 5s for content script to extract and send
      };

      chrome.tabs.onUpdated.addListener(onUpdated);
    });
  });
}

async function handleStartIrepsAutoSearch(): Promise<object> {
  const tab = await openOrFocusTab(IREPS_ADVANCED_SEARCH_URL);
  if (!tab?.id) return { error: 'Failed to open the IREPS search tab.' };

  const response = await sendToTabWithRetry(
    tab.id,
    { type: 'RUN_IREPS_AUTO_SEARCH' },
    { attempts: 8, initialDelayMs: 2000, betweenDelayMs: 1500 },
  );

  if (response.error) {
    return {
      error:
        `Could not reach the IREPS search driver: ${response.error}. ` +
        `Reload the extension at chrome://extensions, then refresh the IREPS tab.`,
    };
  }
  return { started: true, ...(response.value || {}) };
}

async function handleIrepsAutoSearchSubmitted(payload: any): Promise<void> {
  console.log('[Ext] IREPS auto-search form submitted', payload);
  const settings = await getSettings();
  if (settings.notificationsEnabled) {
    chrome.notifications.create({
      type: 'basic',
      iconUrl: 'icons/icon-48.png',
      title: 'IREPS search submitted',
      message: 'Search submitted. Results will be extracted when the page loads.',
    });
  }
}

// --- Document Hunt (Phase 2) ---

async function handleStartDocumentHunt(): Promise<object> {
  if (isDocumentHunting) {
    return { error: 'A document hunt is already in progress.' };
  }
  const stored = await chrome.storage.local.get('pendingDocumentHunt');
  const tenders: TenderData[] = stored.pendingDocumentHunt || [];
  if (tenders.length === 0) {
    return { error: 'No tenders pending document hunt. Run a scrape first.' };
  }

  isDocumentHunting = true;
  // Run async (don't block the message channel)
  processDocumentHunt(tenders)
    .catch((err) => console.error('[Ext] Document hunt fatal:', err))
    .finally(() => { isDocumentHunting = false; });

  return { started: true, count: tenders.length };
}

/** Single-tender on-demand doc fetch (e.g. web-platform tender-detail page). Awaits completion. */
async function handleHuntDocsForTender(tender: TenderData | undefined): Promise<object> {
  if (!tender) {
    return { error: 'No tender provided.' };
  }
  if (isDocumentHunting) {
    return { error: 'A document hunt is already in progress.' };
  }

  isDocumentHunting = true;
  try {
    const result = await processDocumentHunt([tender], { clearQueue: false, forceDownload: true });
    // `completed` (not `started`) — unlike the fire-and-forget batch handler,
    // this awaits the full fetch, so a caller reading the response knows the
    // hunt is already DONE (isDocumentHunting is back to false) and need not poll.
    return { completed: true, ...result };
  } catch (err) {
    console.error('[Ext] Single-tender document hunt fatal:', err);
    return { error: (err as Error).message };
  } finally {
    isDocumentHunting = false;
  }
}

console.log('[Ext] Service worker started');

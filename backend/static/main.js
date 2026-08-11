document.addEventListener('DOMContentLoaded', () => {
    lucide.createIcons();

    // ==========================================================================
    // CURSOR-FOLLOW MAGNETIC SPOTLIGHT (Smooth 60fps Lerp)
    // ==========================================================================
    const cursorGlow = document.getElementById('cursorGlow');
    let mouseX = window.innerWidth / 2;
    let mouseY = window.innerHeight / 2;
    let glowX  = mouseX;
    let glowY  = mouseY;
    const LERP = 0.08; // Lower = silkier trail, Higher = snappier follow

    document.addEventListener('mousemove', (e) => {
        mouseX = e.clientX;
        mouseY = e.clientY;
    });

    function animateCursor() {
        // Smooth lerp interpolation towards real mouse position
        glowX += (mouseX - glowX) * LERP;
        glowY += (mouseY - glowY) * LERP;
        if (cursorGlow) {
            cursorGlow.style.left = glowX + 'px';
            cursorGlow.style.top  = glowY + 'px';
        }
        requestAnimationFrame(animateCursor);
    }
    animateCursor();

    // ==========================================================================
    // DOM Element References
    // ==========================================================================
    const urlInput           = document.getElementById('urlInput');
    const scrapeBtn        = document.getElementById('scrapeBtn');
    const loader           = document.getElementById('loader');
    const resultsSection   = document.getElementById('resultsSection');
    const imageGrid        = document.getElementById('imageGrid');
    const imageCount       = document.getElementById('imageCount');
    const downloadBtn      = document.getElementById('downloadBtn');
    const downloadZipBtn   = document.getElementById('downloadZipBtn');
    const autoscrollToggle = document.getElementById('autoscrollToggle');
    const filterSelect     = document.getElementById('filterSelect');
    
    // Theme Switch
    const themeToggleBtn   = document.getElementById('themeToggleBtn');
    
    // Selection Bar
    const selectionBar     = document.getElementById('selectionBar');
    const selectionInfo    = document.getElementById('selectionInfo');
    const selectAllBtn     = document.getElementById('selectAllBtn');
    const clearSelectionBtn= document.getElementById('clearSelectionBtn');
    
    // Download Modal (Popup)
    const downloadModal       = document.getElementById('downloadModal');
    const downloadModalTitle  = document.getElementById('downloadModalTitle');
    const downloadModalStatus = document.getElementById('downloadModalStatus');
    const progressBarFill     = document.getElementById('progressBarFill');
    const statProgress        = document.getElementById('statProgress');
    const statSpeed           = document.getElementById('statSpeed');
    const statEta             = document.getElementById('statEta');
    const downloadLog         = document.getElementById('downloadLog');
    const cancelDownloadBtn   = document.getElementById('cancelDownloadBtn');
    
    // Lightbox Carousel
    const lightboxModal       = document.getElementById('lightboxModal');
    const lightboxTitle       = document.getElementById('lightboxTitle');
    const lightboxImg         = document.getElementById('lightboxImg');
    const lightboxCloseBtn    = document.getElementById('lightboxCloseBtn');
    const lightboxPrevBtn     = document.getElementById('lightboxPrevBtn');
    const lightboxNextBtn     = document.getElementById('lightboxNextBtn');
    const lightboxDownloadBtn = document.getElementById('lightboxDownloadBtn');
    const lightboxMeta        = document.getElementById('lightboxMeta');
    const lightboxCounter     = document.getElementById('lightboxCounter');

    // ==========================================================================
    // State Management
    // ==========================================================================
    let allImages       = [];
    let filteredImages  = [];
    let selectedUrls    = new Set();
    let currentLightboxIdx = -1;
    
    // Downloader control state
    let downloadAborted = false;
    let activeDownloads = 0;

    // ==========================================================================
    // Theme Switcher Controller
    // ==========================================================================
    const savedTheme = localStorage.getItem('theme') || 'dark';
    document.documentElement.setAttribute('data-theme', savedTheme);
    updateThemeUI(savedTheme);

    themeToggleBtn.addEventListener('click', () => {
        const currentTheme = document.documentElement.getAttribute('data-theme');
        const nextTheme = currentTheme === 'dark' ? 'light' : 'dark';
        document.documentElement.setAttribute('data-theme', nextTheme);
        localStorage.setItem('theme', nextTheme);
        updateThemeUI(nextTheme);
    });

    function updateThemeUI(theme) {
        if (theme === 'light') {
            themeToggleBtn.innerHTML = '<i data-lucide="moon"></i>';
            themeToggleBtn.setAttribute('title', 'Switch to MEDIA FIREWALL Dark Theme');
        } else {
            themeToggleBtn.innerHTML = '<i data-lucide="sun"></i>';
            themeToggleBtn.setAttribute('title', 'Switch to MEDIA FIREWALL Light Theme');
        }
        lucide.createIcons();
    }

    // ==========================================================================
    // Trigger Scrape on Enter
    // ==========================================================================
    urlInput.addEventListener('keydown', e => {
        if (e.key === 'Enter') scrapeBtn.click();
    });

    // ==========================================================================
    // Scraping Controller
    // ==========================================================================
    scrapeBtn.addEventListener('click', async () => {
        const url = urlInput.value.trim();
        if (!url) return alert('Please enter a valid URL');

        resultsSection.classList.add('hidden');
        loader.classList.remove('hidden');
        imageGrid.innerHTML = '';
        selectedUrls.clear();
        updateSelectionUI();

        const autoscroll = autoscrollToggle.checked;
        loader.querySelector('p').textContent = autoscroll
            ? 'Deep scraping with auto-scroll... (This may take up to 30s)'
            : 'Scraping page elements...';

        try {
            const response = await fetch('/api/scrape', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ url, autoscroll })
            });
            const data = await response.json();
            if (data.error) throw new Error(data.error);

            allImages = data.images;
            filterSelect.value = 'all';
            applyFilter();

            loader.classList.add('hidden');
            resultsSection.classList.remove('hidden');
            lucide.createIcons();
        } catch (err) {
            alert('Scraping failed: ' + err.message);
            loader.classList.add('hidden');
        }
    });

    // ==========================================================================
    // Filtering Controller
    // ==========================================================================
    filterSelect.addEventListener('change', () => {
        applyFilter();
    });

    function applyFilter() {
        const activeFilter = filterSelect.value;

        if (activeFilter === 'all') {
            filteredImages = allImages;
        } else if (activeFilter === 'hd') {
            filteredImages = allImages.filter(img =>
                (img.width !== 'Original' && img.width >= 1080) ||
                (img.height !== 'Original' && img.height >= 1080) ||
                img.alt === 'Highest Quality Asset' ||
                img.alt === 'Search Source (Original)'
            );
        } else {
            filteredImages = allImages.filter(img => {
                const ext = img.url.split('.').pop().split('?')[0].toLowerCase();
                if (activeFilter === 'jpg') return ext === 'jpg' || ext === 'jpeg';
                return ext === activeFilter;
            });
        }
        imageCount.textContent = `Found ${filteredImages.length} images`;
        
        // Clean out selected URLs that are no longer in the filtered set
        const filteredUrlsSet = new Set(filteredImages.map(img => img.url));
        selectedUrls.forEach(url => {
            if (!filteredUrlsSet.has(url)) {
                selectedUrls.delete(url);
            }
        });
        
        updateSelectionUI();
        displayImages(filteredImages);
    }

    // ==========================================================================
    // Selection Engine / UI Updates
    // ==========================================================================
    function updateSelectionUI() {
        const count = selectedUrls.size;
        if (count > 0) {
            selectionBar.classList.remove('hidden');
            selectionInfo.textContent = `${count} image(s) selected`;
            downloadBtn.querySelector('span').textContent = `Download Selected (${count})`;
            downloadZipBtn.querySelector('span').textContent = `Download ZIP (${count})`;
        } else {
            selectionBar.classList.add('hidden');
            downloadBtn.querySelector('span').textContent = `Download Files`;
            downloadZipBtn.querySelector('span').textContent = `Download ZIP`;
        }
    }

    selectAllBtn.addEventListener('click', () => {
        filteredImages.forEach(img => selectedUrls.add(img.url));
        updateSelectionUI();
        // Update all cards visually
        document.querySelectorAll('.img-card').forEach(card => card.classList.add('selected'));
    });

    clearSelectionBtn.addEventListener('click', () => {
        selectedUrls.clear();
        updateSelectionUI();
        // Update all cards visually
        document.querySelectorAll('.img-card').forEach(card => card.classList.remove('selected'));
    });

    // ==========================================================================
    // Display Grid Renderer
    // ==========================================================================
    function displayImages(images) {
        imageGrid.innerHTML = '';
        if (images.length === 0) {
            imageGrid.innerHTML = `<p style="color:var(--text-dim);grid-column:1/-1;text-align:center;padding:3rem;font-weight:600;">No images match this filter.</p>`;
            return;
        }

        images.forEach((img, idx) => {
            const card = document.createElement('div');
            const isSelected = selectedUrls.has(img.url);
            card.className = `img-card ${isSelected ? 'selected' : ''}`;
            card.dataset.index = idx;

            const urlLow = img.url.toLowerCase();
            const hasOrig = urlLow.includes('/orig') || urlLow.includes('/originals/') || urlLow.includes('=s0') || urlLow.includes('original');
            const isLarge = (img.width && img.width !== 'Original' && img.width > 1000) ||
                            (img.height && img.height !== 'Original' && img.height > 1000);
            const isOriginal = (hasOrig || isLarge);

            const proxyUrl = `/api/proxy_download?url=${encodeURIComponent(img.url)}`;

            card.innerHTML = `
                <!-- Custom Checkbox overlay -->
                <div class="card-select-checkbox" title="Select Image">
                    <i data-lucide="check"></i>
                </div>
                
                <img src="${proxyUrl}" alt="${img.alt || 'Image'}" loading="lazy" onerror="if(this.dataset.fallback!='1'){this.dataset.fallback='1';this.src='${img.url.replace(/'/g, "\\'")}';}">
                
                <div class="card-overlay">
                    <span class="badge ${isOriginal ? 'badge-orig' : 'badge-hq'}">
                        ${isOriginal ? 'Original' : 'HQ'}
                    </span>
                    <div class="card-actions">
                        <button class="btn-mini btn-preview" title="View Full Details">
                            <i data-lucide="maximize-2"></i>
                        </button>
                    </div>
                </div>
            `;

            // Event Listeners for Cards
            card.addEventListener('click', (e) => {
                const target = e.target;
                
                // If clicked preview button, trigger Lightbox
                if (target.closest('.btn-preview')) {
                    e.stopPropagation();
                    openLightbox(idx);
                    return;
                }
                
                // Otherwise click toggles selection
                e.preventDefault();
                toggleCardSelection(card, img.url);
            });

            imageGrid.appendChild(card);
        });

        lucide.createIcons();
    }

    function toggleCardSelection(card, url) {
        if (selectedUrls.has(url)) {
            selectedUrls.delete(url);
            card.classList.remove('selected');
        } else {
            selectedUrls.add(url);
            card.classList.add('selected');
        }
        updateSelectionUI();
    }

    // ==========================================================================
    // Telemetry Progress Modal Utilities
    // ==========================================================================
    function showProgressModal(title, initialStatus) {
        downloadAborted = false;
        downloadModalTitle.textContent = title;
        downloadModalStatus.textContent = initialStatus;
        progressBarFill.style.width = '0%';
        statProgress.textContent = '0%';
        statSpeed.textContent = '0.0 MB/s';
        statEta.textContent = '--:--';
        downloadLog.innerHTML = `<div class="log-entry">Session initialized. Allocating workers...</div>`;
        downloadModal.classList.add('active');
    }

    function addLogEntry(text, type = '') {
        const entry = document.createElement('div');
        entry.className = `log-entry ${type}`;
        entry.textContent = text;
        downloadLog.appendChild(entry);
        downloadLog.scrollTop = downloadLog.scrollHeight;
    }

    function updateProgressStats(progressPercentage, speedText, etaText) {
        progressBarFill.style.width = `${progressPercentage}%`;
        statProgress.textContent = `${Math.round(progressPercentage)}%`;
        statSpeed.textContent = speedText;
        statEta.textContent = etaText;
    }

    cancelDownloadBtn.addEventListener('click', () => {
        downloadAborted = true;
        addLogEntry('Cancellation request received. Aborting...', 'fail');
        downloadModalStatus.textContent = 'Cancelling download queue...';
        cancelDownloadBtn.disabled = true;
    });

    // ==========================================================================
    // JS CONCURRENCY PARALLEL DOWNLOADER (Individual Files)
    // ==========================================================================
    downloadBtn.addEventListener('click', async () => {
        // Determine targets: selected set or filtered images
        const targets = selectedUrls.size > 0 
            ? filteredImages.filter(img => selectedUrls.has(img.url))
            : filteredImages;
            
        if (targets.length === 0) return;

        const confirmDownload = confirm(`Download ${targets.length} individual images concurrently?`);
        if (!confirmDownload) return;

        cancelDownloadBtn.disabled = false;
        showProgressModal('Downloading Individual Assets', `Processing 0 of ${targets.length}...`);

        const CONCURRENCY = 6;
        let completed = 0;
        let success = 0;
        let downloadedBytes = 0;
        let currentIndex = 0;
        const startTime = Date.now();
        
        const workers = [];

        async function worker() {
            while (currentIndex < targets.length && !downloadAborted) {
                const myIndex = currentIndex++;
                const img = targets[myIndex];
                
                try {
                    downloadModalStatus.textContent = `Downloading ${completed + 1} of ${targets.length}...`;
                    const proxyUrl = `/api/proxy_download?url=${encodeURIComponent(img.url)}`;
                    
                    const timeStart = Date.now();
                    let response = null;
                    let blob = null;

                    try {
                        response = await fetch(proxyUrl, { redirect: 'follow' });
                        if (response && response.ok) {
                            blob = await response.blob();
                        }
                    } catch (e) {}

                    if (!blob || blob.size === 0) {
                        try {
                            const directRes = await fetch(img.url, { mode: 'cors', redirect: 'follow' });
                            if (directRes && directRes.ok) {
                                blob = await directRes.blob();
                            }
                        } catch (e) {}
                    }
                    
                    let ext = img.url.split('.').pop().split('?')[0].toLowerCase();
                    if (!['jpg', 'jpeg', 'png', 'webp', 'gif', 'avif'].includes(ext)) ext = 'jpg';

                    if (blob && blob.size > 0) {
                        const duration = (Date.now() - timeStart) / 1000;
                        downloadedBytes += blob.size;
                        
                        const objUrl = window.URL.createObjectURL(blob);
                        const a = document.createElement('a');
                        a.href = objUrl;
                        a.download = `asset_${myIndex + 1}_${Math.random().toString(36).substring(2, 8)}.${ext}`;
                        document.body.appendChild(a);
                        a.click();
                        
                        // Give browser time to register download before revoking
                        await new Promise(r => setTimeout(r, 120));
                        window.URL.revokeObjectURL(objUrl);
                        a.remove();
                        
                        success++;
                        addLogEntry(`✔ File ${myIndex + 1} downloaded successfully (${(blob.size / 1024).toFixed(1)} KB)`, 'success');
                    }
                } catch (err) {
                    console.error('Download processing notice for:', img.url, err);
                } finally {
                    completed++;
                    
                    // Throttle worker loop slightly to keep browser UI responsive
                    await new Promise(r => setTimeout(r, 50));
                    
                    // Live Telemetry Calculations
                    const elapsed = (Date.now() - startTime) / 1000;
                    const speed = downloadedBytes / (elapsed || 1);
                    
                    // Format Speed
                    let speedText = '0.0 MB/s';
                    if (speed < 1024) speedText = `${speed.toFixed(0)} B/s`;
                    else if (speed < 1024 * 1024) speedText = `${(speed / 1024).toFixed(1)} KB/s`;
                    else speedText = `${(speed / (1024 * 1024)).toFixed(1)} MB/s`;

                    // Format ETA (Rolling file estimates)
                    let etaText = '--:--';
                    if (completed > 0) {
                        const avgTimePerFile = elapsed / completed;
                        const remainingFiles = targets.length - completed;
                        const etaSecs = avgTimePerFile * remainingFiles;
                        
                        if (etaSecs < 60) etaText = `${Math.ceil(etaSecs)}s`;
                        else {
                            const mins = Math.floor(etaSecs / 60);
                            const secs = Math.ceil(etaSecs % 60);
                            etaText = `${mins}:${secs < 10 ? '0' : ''}${secs}`;
                        }
                    }

                    const progressPercent = (completed / targets.length) * 100;
                    updateProgressStats(progressPercent, speedText, etaText);
                }
            }
        }

        // Spawn initial parallel workers
        const workerCount = Math.min(CONCURRENCY, targets.length);
        for (let i = 0; i < workerCount; i++) {
            workers.push(worker());
        }

        // Wait for all workers to exhaust queue
        await Promise.all(workers);

        if (downloadAborted) {
            addLogEntry('Download queue halted by user request.', 'success');
            downloadModalStatus.textContent = 'Download cancelled.';
        } else {
            addLogEntry(`✨ Finished! All ${targets.length} high-resolution assets processed successfully!`, 'success');
            downloadModalStatus.textContent = 'All downloads completed!';
        }
        
        statEta.textContent = 'Finished';
        cancelDownloadBtn.textContent = 'Close Panel';
        cancelDownloadBtn.className = 'btn-primary';
        cancelDownloadBtn.disabled = false;
        
        // Temporarily redefine the cancel click to just close
        const closeHandler = () => {
            downloadModal.classList.remove('active');
            cancelDownloadBtn.textContent = 'Cancel Download';
            cancelDownloadBtn.className = 'btn-danger';
            cancelDownloadBtn.removeEventListener('click', closeHandler);
        };
        cancelDownloadBtn.addEventListener('click', closeHandler);
    });

    // ==========================================================================
    // FAST SERVER-SIDE ZIP ENGINE WITH FETCH STREAM TELEMETRY
    // ==========================================================================
    downloadZipBtn.addEventListener('click', async () => {
        const targets = selectedUrls.size > 0 
            ? filteredImages.filter(img => selectedUrls.has(img.url)).map(img => img.url)
            : filteredImages.map(img => img.url);
            
        if (targets.length === 0) return;

        cancelDownloadBtn.disabled = false;
        showProgressModal('Compiling ZIP Archive', 'Sending request to parallel compilation server...');
        addLogEntry('Parallel worker pool spinning up on backend...', 'success');

        try {
            const startTime = Date.now();
            
            const response = await fetch('/api/download', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ urls: targets })
            });

            if (!response.ok) throw new Error(`Server returned HTTP ${response.status}`);
            
            addLogEntry('Backend parallel compilation complete. Streaming zip archive to browser...', 'success');
            downloadModalStatus.textContent = 'Streaming ZIP archive...';
            
            const reader = response.body.getReader();
            const contentLength = +response.headers.get('Content-Length') || 0;
            
            let receivedLength = 0;
            let chunks = [];
            
            // Read streamed response body chunks
            while (true) {
                if (downloadAborted) {
                    reader.cancel();
                    throw new Error('Streaming cancelled by user');
                }
                
                const { done, value } = await reader.read();
                if (done) break;
                
                chunks.push(value);
                receivedLength += value.length;
                
                const elapsed = (Date.now() - startTime) / 1000;
                const speed = receivedLength / elapsed;
                
                // Speed format
                let speedText = '0.0 MB/s';
                if (speed < 1024 * 1024) speedText = `${(speed / 1024).toFixed(1)} KB/s`;
                else speedText = `${(speed / (1024 * 1024)).toFixed(1)} MB/s`;

                // Progress percentage (if Content-Length is provided)
                let percent = 0;
                let etaText = '--:--';
                
                if (contentLength) {
                    percent = (receivedLength / contentLength) * 100;
                    const remainingBytes = contentLength - receivedLength;
                    const etaSecs = remainingBytes / speed;
                    etaText = etaSecs < 60 ? `${Math.ceil(etaSecs)}s` : `${Math.floor(etaSecs / 60)}m`;
                } else {
                    // Fallback visual pulse progress
                    percent = Math.min((receivedLength / (5 * 1024 * 1024)) * 100, 99); // Estimate progress up to 5MB
                    etaText = 'Streaming';
                }
                
                downloadModalStatus.textContent = `Received ${(receivedLength / (1024 * 1024)).toFixed(2)} MB...`;
                updateProgressStats(percent, speedText, etaText);
            }

            // Reconstruct downloaded chunks into blob
            const blob = new Blob(chunks, { type: 'application/zip' });
            const objUrl = window.URL.createObjectURL(blob);
            
            const a = document.createElement('a');
            a.href = objUrl;
            a.download = `scraper_assets_${Date.now().toString().slice(-6)}.zip`;
            document.body.appendChild(a);
            a.click();
            window.URL.revokeObjectURL(objUrl);
            a.remove();
            
            addLogEntry(`✔ ZIP compiled and downloaded successfully (${(blob.size / (1024 * 1024)).toFixed(2)} MB)`, 'success');
            downloadModalStatus.textContent = 'ZIP download completed!';
            updateProgressStats(100, '0.0 B/s', 'Finished');
        } catch (err) {
            console.error('ZIP compilation download failed:', err);
            addLogEntry(`✖ Archive creation failed: ${err.message}`, 'fail');
            downloadModalStatus.textContent = downloadAborted ? 'ZIP compilation aborted.' : 'Error creating ZIP.';
        } finally {
            cancelDownloadBtn.textContent = 'Close Panel';
            cancelDownloadBtn.className = 'btn-primary';
            cancelDownloadBtn.disabled = false;
            
            const closeHandler = () => {
                downloadModal.classList.remove('active');
                cancelDownloadBtn.textContent = 'Cancel Download';
                cancelDownloadBtn.className = 'btn-danger';
                cancelDownloadBtn.removeEventListener('click', closeHandler);
            };
            cancelDownloadBtn.addEventListener('click', closeHandler);
        }
    });

    // ==========================================================================
    // INTERACTIVE CAROUSEL LIGHTBOX & INSPECTOR
    // ==========================================================================
    function openLightbox(index) {
        currentLightboxIdx = index;
        updateLightboxContent();
        lightboxModal.classList.add('active');
        document.body.style.overflow = 'hidden'; // Lock base scroll
    }

    function closeLightbox() {
        lightboxModal.classList.remove('active');
        document.body.style.overflow = ''; // Restore base scroll
        lightboxImg.src = ''; // Deallocate image link memory
    }

    function updateLightboxContent() {
        if (currentLightboxIdx < 0 || currentLightboxIdx >= filteredImages.length) return;
        
        const img = filteredImages[currentLightboxIdx];
        const proxyUrl = `/api/proxy_download?url=${encodeURIComponent(img.url)}`;
        
        // Show loading placeholder visual state
        lightboxImg.src = proxyUrl;
        lightboxTitle.textContent = img.alt || 'High-Resolution Visual Asset';
        lightboxCounter.textContent = `${currentLightboxIdx + 1} of ${filteredImages.length}`;
        
        // Initial meta text
        let ext = img.url.split('.').pop().split('?')[0].toUpperCase();
        if (ext.length > 4 || !ext.match(/^[A-Z0-9]+$/)) ext = 'JPG/PNG';
        lightboxMeta.textContent = `Dimensions: ${img.width} x ${img.height} | Type: ${ext}`;
        
        // Fetch real dimensions if not populated
        if (img.width === 'Original' || !img.width) {
            const tempImg = new Image();
            tempImg.onload = function() {
                lightboxMeta.textContent = `Dimensions: ${tempImg.naturalWidth} x ${tempImg.naturalHeight} | Type: ${ext}`;
                // Cache it locally so subsequent loads are immediate
                img.width = tempImg.naturalWidth;
                img.height = tempImg.naturalHeight;
            };
            tempImg.src = proxyUrl;
        }
    }

    // Lightbox click events
    lightboxCloseBtn.addEventListener('click', closeLightbox);
    
    lightboxPrevBtn.addEventListener('click', () => {
        if (filteredImages.length <= 1) return;
        currentLightboxIdx = (currentLightboxIdx - 1 + filteredImages.length) % filteredImages.length;
        updateLightboxContent();
    });

    lightboxNextBtn.addEventListener('click', () => {
        if (filteredImages.length <= 1) return;
        currentLightboxIdx = (currentLightboxIdx + 1) % filteredImages.length;
        updateLightboxContent();
    });

    lightboxDownloadBtn.addEventListener('click', () => {
        if (currentLightboxIdx < 0 || currentLightboxIdx >= filteredImages.length) return;
        const img = filteredImages[currentLightboxIdx];
        
        // Trigger single download trigger
        const a = document.createElement('a');
        a.href = `/api/proxy_download?url=${encodeURIComponent(img.url)}`;
        let ext = img.url.split('.').pop().split('?')[0].toLowerCase();
        if (!['jpg', 'jpeg', 'png', 'webp', 'gif'].includes(ext)) ext = 'jpg';
        a.download = `preview_download_${Date.now().toString().slice(-4)}.${ext}`;
        document.body.appendChild(a);
        a.click();
        a.remove();
    });

    // Close lightbox on clicking outside image container
    lightboxModal.addEventListener('click', (e) => {
        if (e.target === lightboxModal || e.target.closest('.lightbox-body') && !e.target.closest('.lightbox-image-container') && !e.target.closest('.btn-nav')) {
            closeLightbox();
        }
    });

    // ==========================================================================
    // Keyboard Controller (Lightbox Arrows and Escape)
    // ==========================================================================
    document.addEventListener('keydown', (e) => {
        if (!lightboxModal.classList.contains('active')) return;
        
        if (e.key === 'Escape') {
            closeLightbox();
        } else if (e.key === 'ArrowLeft') {
            lightboxPrevBtn.click();
        } else if (e.key === 'ArrowRight') {
            lightboxNextBtn.click();
        }
    });
});

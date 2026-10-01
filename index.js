const express = require('express')
const { default: makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } = require('@whiskeysockets/baileys')
const pino = require('pino')
const QRCode = require('qrcode')
const https = require('https')
const http = require('http')

const app = express()
app.use(express.json())

const PORT = process.env.PORT || 3000
const AUTH_DIR = '/app/wa_auth'

// ─────────────────────────────────────────────
// TARGET GROUP (set these in Railway Variables)
// TARGET_GROUP      = exact group name as it appears in WhatsApp
// TARGET_GROUP_JID  = optional, the group JID if you already know it
//                     (find it with /list-groups-full after connecting)
// ─────────────────────────────────────────────
const TARGET_GROUP = (process.env.TARGET_GROUP || '').trim()
const TARGET_GROUP_JID = (process.env.TARGET_GROUP_JID || '').trim()

let sock = null
let qrCode = null
let isConnected = false
let groupJids = {}

async function connectToWhatsApp() {
    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR)
    const { version } = await fetchLatestBaileysVersion()

    sock = makeWASocket({
        version,
        logger: pino({ level: 'silent' }),
        printQRInTerminal: true,
        auth: state,
        browser: ['RayGoldSignals2', 'Chrome', '1.0.0'],
    })

    sock.ev.on('creds.update', saveCreds)

    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect, qr } = update

        if (qr) {
            console.log('📱 QR code generated — scan it!')
            qrCode = await QRCode.toDataURL(qr)
            isConnected = false
        }

        if (connection === 'close') {
            const shouldReconnect = lastDisconnect?.error?.output?.statusCode !== DisconnectReason.loggedOut
            console.log('❌ Connection closed. Reconnecting:', shouldReconnect)
            isConnected = false
            qrCode = null
            if (shouldReconnect) setTimeout(connectToWhatsApp, 3000)
        }

        if (connection === 'open') {
            console.log('✅ WhatsApp connected!')
            isConnected = true
            qrCode = null
            await findTargetGroup()
        }
    })
}

async function findTargetGroup() {
    groupJids = {}

    if (!TARGET_GROUP && !TARGET_GROUP_JID) {
        console.log('⚠️ No TARGET_GROUP or TARGET_GROUP_JID variable set. Set one in Railway Variables.')
        return
    }

    // If a JID is set directly, use it and we are done
    if (TARGET_GROUP_JID) {
        const name = TARGET_GROUP || TARGET_GROUP_JID
        groupJids[name] = TARGET_GROUP_JID
        console.log(`📌 Using JID from variable: "${name}" (${TARGET_GROUP_JID})`)
        return
    }

    // Otherwise scan for the group by name
    try {
        const groups = await sock.groupFetchAllParticipating()
        for (const [jid, group] of Object.entries(groups)) {
            const name = group.subject?.trim()
            if (name === TARGET_GROUP) {
                groupJids[name] = jid
                console.log(`✅ Found by scan: "${name}" (${jid})`)
                return
            }
        }
        console.log(`⚠️ Group not found by name: "${TARGET_GROUP}". Check the exact name with /list-groups`)
    } catch (err) {
        console.error('❌ Error scanning groups:', err.message)
    }
}

async function fetchMediaBuffer(url) {
    return new Promise((resolve, reject) => {
        const client = url.startsWith('https') ? https : http
        client.get(url, (res) => {
            if (res.statusCode && res.statusCode >= 400) {
                reject(new Error(`HTTP ${res.statusCode} fetching ${url}`))
                return
            }
            const chunks = []
            res.on('data', chunk => chunks.push(chunk))
            res.on('end', () => resolve(Buffer.concat(chunks)))
            res.on('error', reject)
        }).on('error', reject)
    })
}

// ─────────────────────────────────────────────
// ROUTES
// ─────────────────────────────────────────────

app.get('/qr', async (req, res) => {
    if (isConnected) {
        const foundGroups = Object.entries(groupJids).map(([name, jid]) => `${name}: ${jid}`).join('<br>')
        return res.send(`
            <html><body style="font-family:Arial;text-align:center;padding:50px">
            <h2>✅ WhatsApp Connected!</h2>
            <h3>Target group:</h3>
            <p>${foundGroups || '⚠️ Not found yet — check TARGET_GROUP variable, then open /refresh-groups'}</p>
            </body></html>
        `)
    }
    if (qrCode) {
        return res.send(`
            <html><body style="font-family:Arial;text-align:center;padding:50px">
            <h2>📱 Scan QR Code with WhatsApp</h2>
            <p>Open WhatsApp → Settings → Linked Devices → Link a Device</p>
            <img src="${qrCode}" style="width:300px;height:300px"/>
            <p>Refresh this page after scanning</p>
            </body></html>
        `)
    }
    return res.send(`
        <html><body style="font-family:Arial;text-align:center;padding:50px">
        <h2>⏳ Generating QR code...</h2>
        <p>Please wait a few seconds and refresh</p>
        </body></html>
    `)
})

app.post('/send', async (req, res) => {
    const { message, group, image_url, video_url, voice_url, image_data, exclude } = req.body
    if (!message && !image_data && !image_url && !video_url && !voice_url) return res.status(400).json({ error: 'no message or media' })
    if (!isConnected) return res.status(503).json({ error: 'WhatsApp not connected' })

    if (Object.keys(groupJids).length === 0) await findTargetGroup()

    // This service only has ONE target group.
    // If the bot asks for a specific group that is not ours, skip quietly.
    // If the bot excludes a group that is not ours, we still send.
    let targets = Object.entries(groupJids)
    if (group) {
        targets = targets.filter(([name]) => name === group)
        if (targets.length === 0) {
            console.log(`⏭️ Skipping — requested group "${group}" is not this service's target`)
            return res.json({ status: 'ok', skipped: `not my group: ${group}` })
        }
    }
    if (exclude) {
        targets = targets.filter(([name]) => name !== exclude)
        if (targets.length === 0) {
            console.log(`⏭️ Skipping — target group is excluded: ${exclude}`)
            return res.json({ status: 'ok', skipped: `excluded: ${exclude}` })
        }
    }

    if (targets.length === 0) {
        return res.status(503).json({ error: 'target group not found yet — check TARGET_GROUP variable and /refresh-groups' })
    }

    // Accept image as base64 (preferred — no URL fetch needed) or as URL
    let imageBuffer = null
    if (image_data) {
        try {
            imageBuffer = Buffer.from(image_data, 'base64')
            console.log(`📷 Image from base64: ${imageBuffer.length} bytes`)
        } catch (err) {
            console.log(`⚠️ Could not decode base64 image: ${err.message}`)
        }
    } else if (image_url) {
        try {
            imageBuffer = await fetchMediaBuffer(image_url)
            console.log(`📷 Image fetched: ${image_url} (${imageBuffer.length} bytes)`)
        } catch (err) {
            console.log(`⚠️ Could not fetch image, sending text only: ${err.message}`)
        }
    }

    // Fetch video if provided
    let videoBuffer = null
    if (video_url) {
        try {
            videoBuffer = await fetchMediaBuffer(video_url)
            console.log(`🎥 Video fetched: ${video_url} (${videoBuffer.length} bytes)`)
        } catch (err) {
            console.log(`⚠️ Could not fetch video, falling back: ${err.message}`)
        }
    }

    // Fetch voice note if provided
    let voiceBuffer = null
    if (voice_url) {
        try {
            voiceBuffer = await fetchMediaBuffer(voice_url)
            console.log(`🎙️ Voice fetched: ${voice_url} (${voiceBuffer.length} bytes)`)
        } catch (err) {
            console.log(`⚠️ Could not fetch voice, falling back: ${err.message}`)
        }
    }

    const results = {}
    for (const [name, jid] of targets) {
        try {
            if (voiceBuffer) {
                // ptt:true renders as a proper WhatsApp voice note
                await sock.sendMessage(jid, { audio: voiceBuffer, ptt: true, mimetype: 'audio/ogg; codecs=opus' })
                if (message && message.trim()) {
                    await sock.sendMessage(jid, { text: message })
                }
            } else if (videoBuffer) {
                await sock.sendMessage(jid, { video: videoBuffer, caption: message })
            } else if (imageBuffer) {
                await sock.sendMessage(jid, { image: imageBuffer, caption: message })
            } else {
                await sock.sendMessage(jid, { text: message })
            }
            results[name] = 'sent ✅'
            console.log(`✅ Sent to: ${name}`)
        } catch (err) {
            results[name] = `failed ❌: ${err.message}`
            console.error(`❌ Failed to send to ${name}:`, err.message)
        }
    }

    return res.json({ status: 'ok', results })
})

app.get('/status', (req, res) => {
    res.json({
        connected: isConnected,
        target_group: TARGET_GROUP || null,
        target_group_jid: TARGET_GROUP_JID || null,
        groups: groupJids
    })
})

app.get('/list-groups', async (req, res) => {
    try {
        const groups = await sock.groupFetchAllParticipating()
        const list = Object.values(groups).map(g => g.subject)
        res.json({ total: list.length, groups: list })
    } catch (err) {
        res.status(500).json({ error: err.message })
    }
})

app.get('/list-groups-full', async (req, res) => {
    try {
        const groups = await sock.groupFetchAllParticipating()
        const list = Object.entries(groups).map(([jid, g]) => ({
            jid,
            name: g.subject,
            isCommunity: g.isCommunity,
            isCommunityAnnounce: g.isCommunityAnnounce,
            linkedParent: g.linkedParent
        }))
        res.json({ total: list.length, groups: list })
    } catch (err) {
        res.status(500).json({ error: err.message })
    }
})

app.get('/refresh-groups', async (req, res) => {
    await findTargetGroup()
    res.json({ groups: groupJids })
})

app.get('/', (req, res) => {
    res.json({ status: 'RayWhatsApp 2 running ✅', connected: isConnected, groups: Object.keys(groupJids) })
})

// ─────────────────────────────────────────────
// START
// ─────────────────────────────────────────────

app.listen(PORT, () => {
    console.log(`🚀 RayWhatsApp 2 server running on port ${PORT}`)
    connectToWhatsApp()
})

---
type: runbook
title: Havi számlázási folyamat
status: active
created: 2026-04-05
updated: 2026-09-30
tags: [runbook, számlázás, pénzügy]
aliases: [havi számlázás menete]
---
# Havi számlázási folyamat

Minden hónap utolsó munkanapján futtatom végig. Nagyjából egy óra, ha az idő már fel van jegyezve.

## Előkészítés
1. Az időnyilvántartásból exportálom a hónap óráit ügyfelenként.
2. Összevetem a megállapodott keretösszeggel; ha eltér, előbb egyeztetek az ügyféllel.
3. Megkérem a kapcsolattartót a teljesítésigazolás aláírására, ha a szerződés ezt kéri.

## Számlázás
A számlázás a számlázóprogramban történik, sablonból. A számla megjegyzésébe beírom a projekt nevét és a megrendelés számát. A kiállítása után PDF-et mentek a `~/files` megfelelő mappájába, és elküldöm az ügyfélnek, de csak a jóváhagyás után: a küldés a kézi lépés, azt nem automatizálom.

## Követés
A fizetési határidőt a naptárba teszem. Ha a határidő lejár, a következő munkanapon emlékeztetőt küldök. Az áfa-bevallás időpontjait az [[atalanyado-es-afa]] jegyzet gyűjti.

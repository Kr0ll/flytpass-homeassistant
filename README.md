# Flyt bombrikke for Home Assistant

Uoffisiell integrasjon som henter passeringer for bombrikker hos
[Flyt](https://flytpass.no) via MinSide-API-et.

Integrasjonen logger inn på Flyt MinSide med jevne mellomrom og henter
brikkene, bilene og de siste bompasseringene dine. Den lager sensorer per bil:
siste passering, bomstasjon, pris og sum for måneden, pluss saldo per avtale.
Den sender også en hendelse for hver ny passering, som du kan bruke til varsler
og automasjoner.

> 🤖 **100 % vibecoded med Claude.** Hele integrasjonen, fra kartlegging av
> API-et til kode og feilretting, er laget i en samtale med
> [Claude Code](https://claude.com/claude-code). Se
> [Slik er det laget](#slik-er-det-laget).

> ⚠️ Ikke tilknyttet eller godkjent av Flyt. API-et er ikke offentlig
> dokumentert og kan endres uten varsel.

*English: Unofficial Home Assistant integration for Norwegian Flyt toll tags
(bombrikke). It polls Flyt MinSide for your tags, vehicles and recent toll
passings, and exposes them as sensors and a `flyt_new_passing` event. It was
entirely "vibecoded" with Claude Code.*

## Innlogging

MinSide bruker Azure AD B2C. BankID kan ikke automatiseres, og tokenene
nettsiden får utdelt utløper etter 24 timer. Integrasjonen logger derfor inn
med **e-post og passord**, slik "Har du ikke BankID? Trykk her" gjør på
innloggingssiden. Den fornyer tokenet selv og logger inn på nytt ved behov.

Brukeren må ha tilgang til kundeforholdet. Det gir du under
**MinSide → Tilgangsstyring → Gi tilgang**. Har du glemt passordet til
e-postbrukeren, kan du nullstille det via "Glemt passord" på innloggingssiden.

## Installasjon

### Med HACS (anbefalt)

1. I Home Assistant: **HACS → ⋮ (øverst til høyre) → Egendefinerte repositorier**.
2. Lim inn `https://github.com/Kr0ll/flytpass-homeassistant` og velg kategori
   **Integrasjon**.
3. Søk opp **Flyt bombrikke** i HACS, last den ned og start Home Assistant på nytt.
4. Gå til **Innstillinger → Enheter og tjenester → Legg til integrasjon → Flyt bombrikke**
   og logg inn med e-post og passord.

[![Åpne i HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Kr0ll&repository=flytpass-homeassistant&category=integration)

### Manuelt

Kopier `custom_components/flyt` til `config/custom_components/flyt`, start Home
Assistant på nytt og legg til integrasjonen som over.

## Hva du får

Én enhet per aktive brikke (navngitt etter registreringsnummer og bil), med:

| Sensor | Beskrivelse |
|---|---|
| Siste passering | Tidspunkt (attributter: stasjon, operatør, pris, skiltnummer, de 10 siste passeringene) |
| Siste bomstasjon | Navn på stasjonen |
| Pris siste passering | NOK |
| Bompenger denne måneden | Sum NOK, nullstilles den 1. |
| Passeringer denne måneden | Antall |

I tillegg får du en **Saldo**-sensor per aktive avtale (negativ betyr at du skylder Flyt).

Standard oppdateringsintervall er 30 minutter. Du kan endre det under integrasjonens *Konfigurer*.
Merk at bompasseringer ofte dukker opp hos Flyt timer eller dager etter at de skjedde.

## Hendelse ved ny passering

For hver ny passering sendes hendelsen `flyt_new_passing` med feltene `time`, `station`,
`operator`, `price`, `currency`, `license_plate`, `registration_number`, `vehicle`, `obe_number` og `id`.

```yaml
automation:
  - alias: Varsel ved bompassering
    triggers:
      - trigger: event
        event_type: flyt_new_passing
    actions:
      - action: notify.mobile_app_telefon
        data:
          message: >
            {{ trigger.event.data.registration_number }} passerte
            {{ trigger.event.data.station }} ({{ trigger.event.data.price }} kr)
```

## Test uten Home Assistant

```
pip install aiohttp
python test_flyt.py
```

## Slik er det laget

Prosjektet er laget uten at noen har skrevet kode for hånd. Alt kom fram i en
samtale med Claude Code (Anthropic), der eieren beskrev hva de ville ha, logget
inn selv der det trengtes, og testet resultatet. Omtrent slik gikk det:

1. **Kartlegging av nettsiden.** Claude åpnet MinSide i en nettleser, leste
   JavaScript-koden til siden og fant API-gatewayen
   (`func-flyt-tsp-self-gateway-prod.azurewebsites.net`) og endepunktene.
2. **Observasjon av ekte trafikk.** Eieren logget inn selv med BankID. Claude
   fulgte med på API-kallene siden gjorde, for å se forespørsler og svarformat
   for kunder, brikker og passeringer.
3. **Innloggingsproblemet.** Tokenene Microsoft Azure B2C gir nettsiden,
   utløper 24 timer etter innlogging, uansett hvor ofte de fornyes. BankID kan
   heller ikke automatiseres. Løsningen ble en egen e-postbruker med passord som
   får tilgang via *Tilgangsstyring*. Integrasjonen logger inn som den på samme
   måte som nettsidens skjulte e-post/passord-skjema.
4. **Koding.** Claude skrev integrasjonen: API-klient, config flow,
   koordinator, sensorer, oversettelser og et frittstående testskript.
5. **Feilsøking sammen.** Første test feilet med «Bad Request». Claude
   reproduserte feilen og fant årsaken: `aiohttp` satte anførselstegn rundt
   B2C-cookiene, som har `|` og `:` i navnene. Etter rettingen fungerte
   innloggingen mot en ekte konto.

Passord, BankID og kontoopprettelse ble alltid gjort av eieren selv, ikke av
Claude.

## Begrensninger

- Passeringer dukker ofte opp hos Flyt timer eller dager etter at de skjedde.
- Nye brikker eller biler kommer først med når integrasjonen lastes inn på nytt.
- Passordet lagres i Home Assistant-konfigurasjonen, slik det gjøres for
  andre integrasjoner med brukernavn og passord. Bruk et eget passord for
  e-postbrukeren.

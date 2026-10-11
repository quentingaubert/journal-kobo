# Journal IA quotidien → Kobo (via Instapaper)

Chaque matin vers 5 h 30, GitHub Actions :
1. lit une trentaine de flux RSS (`feeds.yaml`) ;
2. demande à Gemini (offre gratuite) de choisir, dédoublonner et résumer les sujets ;
3. vérifie que chaque sujet cite de vrais articles : les liens sont ajoutés par le code, jamais par l'IA ;
4. publie la page sur GitHub Pages et l'envoie à Instapaper ;
5. la Kobo récupère l'article dans **Plus > Articles**.

Coût : 0 €.

## Installation (une seule fois, environ 15 min)

### 1. Instapaper
- Crée un compte gratuit sur https://www.instapaper.com.
  Si tu t'inscris avec Google ou Apple, définis quand même un **mot de passe** dans les réglages Instapaper : l'API en a besoin.
- Sur la Kobo : **Plus > Articles**, puis connecte ton compte Instapaper.

### 2. Clé Gemini (gratuite)
- Va sur https://aistudio.google.com/apikey, puis *Create API key*. Copie la clé.

### 3. Dépôt GitHub
- Crée un dépôt **public** nommé `journal-kobo` (GitHub Pages est gratuit uniquement pour les dépôts publics).
- Envoie ce dossier dans le dépôt :
  ```
  git init -b main && git add . && git commit -m "Journal IA"
  git remote add origin https://github.com/<ton-pseudo>/journal-kobo.git
  git push -u origin main
  ```

### 4. Réglages du dépôt
- **Settings > Pages > Source** : choisis *GitHub Actions*.
- **Settings > Secrets and variables > Actions > New repository secret**, à créer trois fois :
  - `GEMINI_API_KEY`
  - `INSTAPAPER_USERNAME` (ton e-mail Instapaper)
  - `INSTAPAPER_PASSWORD`

### 5. Premier test
- Va dans **Actions > Journal quotidien > Run workflow**.
- Au bout de 2 à 3 minutes, le journal apparaît dans Instapaper, puis sur la Kobo à la prochaine synchronisation.

## Au quotidien
Il n'y a rien à faire. Si un matin le journal échoue, GitHub t'envoie un e-mail.

## Personnaliser
- **Sources** : modifie `feeds.yaml`.
- **Longueur, ton, rubriques** : modifie le texte `PROMPT` dans `journal.py`.
- **Heure** : modifie la ligne `cron` dans `.github/workflows/journal.yml`. Elle est en heure UTC.
- **Modèle IA** : variable d'environnement `GEMINI_MODELS`. Par défaut : `gemini-flash-latest,gemini-3.8-flash,gemini-3.5-flash-lite`. Les modèles suivants servent de secours si le premier échoue.

## Tester sur l'ordinateur
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python journal.py fetch                       # teste les flux, sans IA
GEMINI_API_KEY=xxx .venv/bin/python journal.py build    # écrit docs/AAAA-MM-JJ.html
```

## À savoir
- Les pages publiées sont **publiques**, à l'adresse `https://<pseudo>.github.io/journal-kobo/`. Ce sont des résumés d'actualité, rien de personnel. Personne ne les trouvera sans connaître l'adresse.
- Chaque numéro est archivé dans `docs/` pendant 30 jours. Ce commit quotidien empêche aussi GitHub de désactiver la tâche planifiée : GitHub coupe les tâches planifiées des dépôts inactifs depuis 60 jours.
- L'offre gratuite de Gemini peut utiliser les requêtes pour entraîner ses modèles. Ici, ce ne sont que des titres d'actualité publics.

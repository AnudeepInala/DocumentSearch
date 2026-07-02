"""
NLP Text Corrector - SpaCy-based text correction for OCR and extracted content
Applies intelligent corrections before indexing in OpenSearch
"""

import re
import logging
from typing import Tuple, Optional, Dict, Set

logger = logging.getLogger(__name__)


class TextCorrector:
    """
    Context-aware text corrector using SpaCy NLP
    
    Features:
    - OCR error correction (common character confusions)
    - Financial term recognition and correction
    - Date and year validation
    - Amount format correction
    - Named entity validation
    """
    
    def __init__(self, model_path: str = None):
        """
        Initialize text corrector with SpaCy model
        
        Args:
            model_path: Path to SpaCy model directory
        """
        self.nlp = None
        self.model_loaded = False
        
        # Try to load SpaCy model
        self._load_model(model_path)
        
        # Build correction dictionaries
        self.financial_phrases = self._build_financial_phrases()
        self.financial_vocabulary = self._build_financial_vocab()
        self.ocr_char_fixes = self._build_ocr_char_fixes()
        
        logger.info(f"TextCorrector initialized (SpaCy loaded: {self.model_loaded})")
    
    def _load_model(self, model_path: str = None) -> bool:
        """Load SpaCy model"""
        try:
            import spacy
            
            # Try loading the model path/name first
            if model_path:
                try:
                    # Try as a model name (e.g., "en_core_web_md") or path
                    self.nlp = spacy.load(model_path)
                    self.model_loaded = True
                    logger.info(f"Loaded SpaCy model: {model_path}")
                    return True
                except Exception as e:
                    logger.debug(f"Could not load model '{model_path}', trying fallbacks: {e}")
            
            # Try default model
            try:
                self.nlp = spacy.load("en_core_web_md")
                self.model_loaded = True
                logger.info("Loaded SpaCy model: en_core_web_md")
                return True
            except Exception:
                pass
            
            # Try smaller model
            try:
                self.nlp = spacy.load("en_core_web_sm")
                self.model_loaded = True
                logger.info("Loaded SpaCy model: en_core_web_sm")
                return True
            except Exception:
                logger.warning("No SpaCy model available, using rule-based corrections only")
                return False
                
        except Exception as e:
            logger.warning(f"SpaCy unavailable/incompatible ({e}), using rule-based corrections only")
            return False
    
    def _build_financial_vocab(self) -> Set[str]:
        """Build comprehensive financial vocabulary"""
        return {
            'cash', 'flow', 'statement', 'balance', 'account', 'revenue',
            'expense', 'depreciation', 'amortization', 'equity', 'asset',
            'liability', 'payable', 'receivable', 'principal', 'adjustment',
            'quarter', 'total', 'increase', 'decrease', 'net', 'operating',
            'investing', 'financing', 'beginning', 'ending', 'activities',
            'provided', 'used', 'loss', 'income', 'payment', 'proceeds',
            'issued', 'repaid', 'business', 'operations', 'purchases',
            'dividends', 'interest', 'taxes', 'accrued', 'prepaid', 'deferred',
            'consolidated', 'subsidiary', 'affiliate', 'shareholders', 'earnings',
            'capital', 'comprehensive', 'restricted', 'unrestricted', 'accumulated',
            'property', 'lease', 'tenant', 'landlord', 'rental', 'mortgage',
            'foreclosure', 'escrow', 'closing', 'deed', 'title', 'lien',
            'appraisal', 'assessed', 'valuation', 'occupancy', 'vacancy',
            'agreement', 'contract', 'executed', 'pursuant', 'thereof', 'hereby',
            'whereas', 'witnesseth', 'covenant', 'warranty', 'indemnify',
            'jurisdiction', 'amendment', 'termination', 'renewal', 'extension',
        }
    
    def _build_financial_phrases(self) -> Dict[str, str]:
        """Build OCR error corrections for financial phrases"""
        return {
            # Balance sheet / Financial statement errors
            'baiance sheet': 'balance sheet',
            'income statment': 'income statement',
            'cash fiow statement': 'cash flow statement',
            'profit and ioss': 'profit and loss',
            'statment of operations': 'statement of operations',
            'sold notlfication': 'sold notification',
            'sold notilication': 'sold notification',
            'notification detalls': 'notification details',
            'net galn analysis': 'net gain analysis',
            'net proceads analysis': 'net proceeds analysis',
            'net proceeds analysls': 'net proceeds analysis',
            
            # Common accounting term errors
            'accounts receivabie': 'accounts receivable',
            'accounts payabie': 'accounts payable',
            'retained earings': 'retained earnings',
            'shareholders equiy': 'shareholders equity',
            'total assests': 'total assets',
            'total liabilites': 'total liabilities',
            'net incme': 'net income',
            'gross proft': 'gross profit',
            'operating expnses': 'operating expenses',
            'pre-tax net galn': 'pre-tax net gain',
            'percent net galn': 'percent net gain',
            'broker commlssion': 'broker commission',
            'attorney fess': 'attorney fees',
            'rent cost basls': 'rent cost basis',
            'sale prlce': 'sale price',
            
            # Real estate terms
            'real esate': 'real estate',
            'property manageent': 'property management',
            'property managernent': 'property management',
            'lease agrrement': 'lease agreement',
            'lease agreernent': 'lease agreement',
            'security deposlt': 'security deposit',
            'rental incone': 'rental income',
            'tenant improvments': 'tenant improvements',
            'investment propertles': 'investment properties',
            'investment propertles group': 'investment properties group',
            'multisite portfollo': 'multisite portfolio',
            
            # Transaction terms
            'purchase prlce': 'purchase price',
            'sale proceds': 'sale proceeds',
            'closing csts': 'closing costs',
            'earnest mony': 'earnest money',
            'down paymet': 'down payment',
            'above referenced': 'above-referenced',
            'on behalf of': 'on behalf of',
            'was sold on': 'was sold on',
            'please be advlsed': 'please be advised',
            'please let me know': 'please let me know',
            'please see the detalls': 'please see the details',
            'has closed and funded': 'has closed and funded',
            'wire confirmatlons': 'wire confirmations',
            'closing statment': 'closing statement',
            
            # Address errors
            'university drlve': 'university drive',
            'ross streel': 'ross street',
            'corsicana streel': 'corsicana street',
            'south streel': 'south street',
            'midland drlve': 'midland drive',
            'main streel': 'main street',
            'main sfreet': 'main street',
            'bell streel': 'bell street',
            'andrews hlghway': 'andrews highway',
            'gause bouievard': 'gause boulevard',
        }
    
    def _build_ocr_char_fixes(self) -> Dict[str, str]:
        """Build character-level OCR error corrections"""
        return {
            # Common OCR character confusions
            '|': 'I',
            # NOTE: '0'→'O' and '1'→'I' REMOVED — they corrupt numeric data
            # e.g. $10,000 → $IO,OOO. Use context-aware replacement instead.
            
            # Ligature fixes
            'ﬁ': 'fi',
            'ﬂ': 'fl',
            'ﬀ': 'ff',
            'ﬃ': 'ffi',
            'ﬄ': 'ffl',
            
            # Smart quotes to regular
            '\u2018': "'",
            '\u2019': "'",
            '\u201c': '"',
            '\u201d': '"',
            
            # Special dashes
            '\u2013': '-',  # en-dash
            '\u2014': '-',  # em-dash
        }
    
    def correct(self, text: str, document_type: str = 'auto') -> Tuple[str, int]:
        """
        Apply multi-stage text corrections
        
        Args:
            text: Raw text to correct
            document_type: Type of document for context-aware corrections
            
        Returns:
            Tuple of (corrected_text, correction_count)
        """
        if not text or not text.strip():
            return text, 0
        
        total_corrections = 0
        corrected = text
        
        try:
            # Stage 1: Fix critical dates and years
            corrected, count = self._fix_dates_years(corrected)
            total_corrections += count
            
            # Stage 2: Fix amounts and currency
            corrected, count = self._fix_amounts(corrected)
            total_corrections += count
            
            # Stage 3: Fix character-level OCR errors
            corrected, count = self._fix_character_errors(corrected)
            total_corrections += count
            
            # Stage 4: Apply dictionary corrections
            corrected, count = self._apply_dictionary(corrected)
            total_corrections += count
            
            # Stage 5: Fix common word patterns
            corrected, count = self._fix_common_patterns(corrected)
            total_corrections += count
            
            # Stage 6: SpaCy-based corrections (if available)
            if self.model_loaded:
                corrected, count = self._apply_spacy_corrections(corrected)
                total_corrections += count
            
            logger.debug(f"Applied {total_corrections} corrections to text")
            
        except Exception as e:
            logger.error(f"Error in text correction: {e}")
        
        return corrected, total_corrections
    
    def _fix_dates_years(self, text: str) -> Tuple[str, int]:
        """Fix invalid dates and year confusion"""
        corrections = 0
        corrected = text
        
        # Fix year confusions (2041 -> 2011, etc.)
        year_patterns = [
            (r'\b2041\b', '2011'),
            (r'\b20ll\b', '2011'),
            (r'\b20I1\b', '2011'),
            (r'\b20l1\b', '2011'),
            (r'\b201l\b', '2011'),
            (r'\b2O11\b', '2011'),
            (r'\b20\|1\b', '2011'),
        ]
        
        for pattern, replacement in year_patterns:
            matches = len(re.findall(pattern, corrected))
            if matches > 0:
                corrected = re.sub(pattern, replacement, corrected)
                corrections += matches
        
        # Fix invalid day numbers in dates
        months_31 = ['January', 'March', 'May', 'July', 'August', 'October', 'December']
        months_30 = ['April', 'June', 'September', 'November']
        
        for month in months_31:
            pattern = rf'({month})\s+([4-9]\d+)'
            matches = re.findall(pattern, corrected)
            for m in matches:
                corrected = re.sub(rf'{month}\s+{m[1]}', f'{month} 31', corrected, count=1)
                corrections += 1
        
        for month in months_30:
            pattern = rf'({month})\s+([4-9]\d+)'
            matches = re.findall(pattern, corrected)
            for m in matches:
                corrected = re.sub(rf'{month}\s+{m[1]}', f'{month} 30', corrected, count=1)
                corrections += 1
        
        # February special case
        pattern = r'(February)\s+([3-9]\d+)'
        matches = re.findall(pattern, corrected)
        for m in matches:
            corrected = re.sub(rf'February\s+{m[1]}', 'February 28', corrected, count=1)
            corrections += 1
        
        return corrected, corrections
    
    def _fix_amounts(self, text: str) -> Tuple[str, int]:
        """Fix currency amount formatting"""
        corrections = 0
        corrected = text
        
        # Fix space-separated thousands ($97 621 -> $97,621)
        pattern1 = r'\$\s*(\d+)\s+(\d{3})\b'
        matches = len(re.findall(pattern1, corrected))
        if matches > 0:
            corrected = re.sub(pattern1, r'$\1,\2', corrected)
            corrections += matches
        
        # Fix parenthetical amounts (($97 621) -> ($97,621))
        pattern2 = r'\(\s*\$?\s*(\d+)\s+(\d{3})\s*\)'
        matches = len(re.findall(pattern2, corrected))
        if matches > 0:
            corrected = re.sub(pattern2, r'($\1,\2)', corrected)
            corrections += matches
        
        # Remove extra spaces after $
        pattern3 = r'\$\s+'
        matches = len(re.findall(pattern3, corrected))
        if matches > 0:
            corrected = re.sub(pattern3, r'$', corrected)
            corrections += matches
        
        return corrected, corrections
    
    def _fix_character_errors(self, text: str) -> Tuple[str, int]:
        """Fix character-level OCR errors"""
        corrections = 0
        corrected = text
        
        # Apply character fixes
        for old, new in self.ocr_char_fixes.items():
            if old in corrected:
                count = corrected.count(old)
                corrected = corrected.replace(old, new)
                corrections += count
        
        # Fix pipe character used as I or l
        # Context: if surrounded by letters, likely I or l
        pattern = r'(\w)\|(\w)'
        matches = len(re.findall(pattern, corrected))
        if matches > 0:
            corrected = re.sub(pattern, r'\1I\2', corrected)
            corrections += matches
        
        # Fix standalone pipes
        corrected = corrected.replace(' | ', ' I ')
        
        return corrected, corrections
    
    def _apply_dictionary(self, text: str) -> Tuple[str, int]:
        """Apply dictionary-based phrase corrections"""
        corrections = 0
        corrected = text
        text_lower = text.lower()
        
        # Apply phrase corrections
        for error, fix in self.financial_phrases.items():
            if error in text_lower:
                # Case-insensitive replacement
                pattern = re.compile(re.escape(error), re.IGNORECASE)
                matches = len(pattern.findall(corrected))
                if matches > 0:
                    corrected = pattern.sub(fix, corrected)
                    corrections += matches
        
        return corrected, corrections
    
    def _fix_common_patterns(self, text: str) -> Tuple[str, int]:
        """Fix common OCR error patterns"""
        corrections = 0
        corrected = text
        
        # Words ending in common OCR errors
        broken_words = {
            'MuItisite': 'Multisite',
            'MuIti': 'Multi',
            'NotIfication': 'Notification',
            'CapItal': 'Capital',
            'FInance': 'Finance',
            'AnaiysIs': 'Analysis',
            'AddItional': 'Additional',
            'InformatIon': 'Information',
            'ConfIrmation': 'Confirmation',
            'CommIssion': 'Commission',
            
            # -ally endings
            'finaIIy': 'finally',
            'usualIy': 'usually',
            'actualIy': 'actually',
            'originalIy': 'originally',
            'realIy': 'really',
            'totalIy': 'totally',
            'generalIy': 'generally',
            'normalIy': 'normally',
            'fulIy': 'fully',
            
            # -tion errors
            'informatlon': 'information',
            'notificatlon': 'notification',
            'confirmatlons': 'confirmations',
            
            # Common OCR errors
            'rnanagement': 'management',
            'agreenent': 'agreement',
            'staternent': 'statement',
            'propertles': 'properties',
            'lnvestment': 'investment',
            'cornmission': 'commission',
            'analysls': 'analysis',
        }
        
        for error, fix in broken_words.items():
            if error in corrected:
                count = corrected.count(error)
                corrected = corrected.replace(error, fix)
                corrections += count
        
        # State abbreviation fixes
        state_fixes = [
            (' lX ', ' TX '),
            (' 1X ', ' TX '),
            ('|TX', ' TX'),
            (' lA ', ' LA '),
            (' 1A ', ' LA '),
            (' M0 ', ' MO '),
        ]
        
        for old, new in state_fixes:
            if old in corrected:
                count = corrected.count(old)
                corrected = corrected.replace(old, new)
                corrections += count
        
        return corrected, corrections
    
    def _apply_spacy_corrections(self, text: str) -> Tuple[str, int]:
        """Apply SpaCy-based corrections using NER and word vectors"""
        if not self.model_loaded or not self.nlp:
            return text, 0
        
        corrections = 0
        corrected = text
        
        try:
            # Process with SpaCy (limit to first 100K chars for performance)
            doc = self.nlp(text[:100000])
            
            # Validate named entities
            for ent in doc.ents:
                if ent.label_ == 'MONEY':
                    # Validate money format
                    money_text = ent.text
                    # Check for common OCR errors in amounts
                    if ' ' in money_text and '$' in money_text:
                        fixed = re.sub(r'\$\s*(\d+)\s+(\d{3})', r'$\1,\2', money_text)
                        if fixed != money_text:
                            corrected = corrected.replace(money_text, fixed)
                            corrections += 1
                
                elif ent.label_ == 'DATE':
                    # Validate date format
                    date_text = ent.text
                    # Check for year OCR errors
                    if '2041' in date_text:
                        fixed = date_text.replace('2041', '2011')
                        corrected = corrected.replace(date_text, fixed)
                        corrections += 1
            
            # Check for potentially misspelled tokens
            for token in doc:
                if not token.is_alpha or len(token.text) < 4:
                    continue
                
                # Check if it's a known word
                if token.has_vector and token.vector_norm > 0:
                    continue  # Known word
                
                # Try to find similar words in vocabulary
                word_lower = token.text.lower()
                if word_lower in self.financial_vocabulary:
                    continue  # Known financial term
                
                # Check for common OCR patterns
                for error, fix in self.financial_phrases.items():
                    if word_lower in error:
                        # Partial match - might be part of a phrase
                        break
            
        except Exception as e:
            logger.warning(f"SpaCy correction error: {e}")
        
        return corrected, corrections


# Singleton instance
_text_corrector: Optional[TextCorrector] = None


def get_text_corrector(model_path: str = None) -> TextCorrector:
    """Get singleton text corrector instance"""
    global _text_corrector
    
    if _text_corrector is None:
        # Get model path from config
        if model_path is None:
            try:
                from core.config_manager import get_config
                config = get_config()
                nlp_config = getattr(config, 'nlp', None)
                if nlp_config:
                    model_path = getattr(nlp_config, 'model_path', 'en_core_web_md')
                else:
                    model_path = 'en_core_web_md'
            except Exception:
                model_path = 'en_core_web_md'
        _text_corrector = TextCorrector(model_path)
    
    return _text_corrector

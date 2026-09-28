from openai import OpenAI
import uuid
import json
import re
from flask import Flask, request, jsonify, Response
from flask_sqlalchemy import SQLAlchemy
from collections import OrderedDict
import pandas as pd
import os
import threading
from flask_admin import Admin
from flask_admin.contrib.sqla import ModelView
from flask_basicauth import BasicAuth
from flask import send_from_directory
from io import BytesIO
import warnings
from dotenv import load_dotenv

load_dotenv()
warnings.filterwarnings("ignore", category=UserWarning, module="flask_admin")

client = OpenAI(api_key=os.environ['OPENAI_API_KEY'])
OPENAI_MODEL = os.environ.get('OPENAI_MODEL', 'gpt-5.4')
os.makedirs("output", exist_ok=True)

# Secure Flask-Admin views
class SecureModelView(ModelView):
	def __init__(self, model, session, basic_auth, **kwargs):
		super().__init__(model, session, **kwargs)
		self.basic_auth = basic_auth

	def is_accessible(self):
		return self.basic_auth.authenticate()

	def inaccessible_callback(self, name, **kwargs):
		return self.basic_auth.challenge()

db = SQLAlchemy()

class OutputDataMore(db.Model):
	__tablename__ = 'output_data_more'
	__table_args__ = {'extend_existing': True}
	id = db.Column(db.Integer, primary_key=True)
	request_id = db.Column(db.String(120), nullable=False)
	status = db.Column(db.String(50), nullable=True)
	filename = db.Column(db.String(120), nullable=True)
	store = db.Column(db.String(120), nullable=False)
	month = db.Column(db.String(50), nullable=False)
	year = db.Column(db.String(50), nullable=False)
	frequent_return_reasons = db.Column(db.Text, nullable=True)
	size_issues = db.Column(db.Text, nullable=True)
	other_considerations = db.Column(db.Text, nullable=True)
	label_issues = db.Column(db.Text, nullable=True)
	noteworthy_products = db.Column(db.Text, nullable=True)

def create_app():
	global basic_auth
	app = Flask(__name__)
	app.config['SECRET_KEY'] = os.environ['FLASK_SECRET_KEY']
	db_path = os.environ.get('DATABASE_PATH', '/app/data/output_data.db')
	app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
	app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
	db.init_app(app)
	os.makedirs("output", exist_ok=True)
	app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024  # 10 MB
	app.config['BASIC_AUTH_USERNAME'] = os.environ['ADMIN_USERNAME']
	app.config['BASIC_AUTH_PASSWORD'] = os.environ['ADMIN_PASSWORD']

	basic_auth = BasicAuth(app)

	admin = Admin(app, name='Admin UI', template_mode='bootstrap3')
	with app.app_context():
		admin.add_view(SecureModelView(OutputDataMore, db.session, basic_auth))
		db.create_all()

	@app.before_request
	def protect_admin():
		if request.path.startswith('/admin'):
			return basic_auth.required(lambda: None)()
	return app


# UTILITY FUNCTIONS BELOW:

def parse_text_to_sections(text):
	section_titles = [
		"FREQUENT RETURN REASONS",
		"SIZE ISSUES",
		"OTHER CONSIDERATIONS",
		"COMMON ISSUES FOR SPECIFIC PRODUCT LABELS",
		"NOTEWORTHY PRODUCTS"
	]
	titles_pattern = "|".join(re.escape(title) for title in section_titles)
	pattern = r"(" + titles_pattern + r")\s*:?\s*(.+?)(?=(?:" + titles_pattern + r")\s*:?|$)"
	matches = re.findall(pattern, text, re.DOTALL)
	parsed_sections = {match[0]: match[1].strip() for match in matches}
	return parsed_sections

def insert_parsed_data_async(request_id, status, original_filename, store, month, year, parsed_data):
	new_entry = OutputDataMore(
		request_id=request_id,
		status=status,
		filename=original_filename,
		store=store,
		month=month,
		year=year,
		frequent_return_reasons=parsed_data.get("FREQUENT RETURN REASONS"),
		size_issues=parsed_data.get("SIZE ISSUES"),
		other_considerations=parsed_data.get("OTHER CONSIDERATIONS"),
		label_issues=parsed_data.get("COMMON ISSUES FOR SPECIFIC PRODUCT LABELS"),
		noteworthy_products=parsed_data.get("NOTEWORTHY PRODUCTS"),
	)
	db.session.add(new_entry)
	try:
		db.session.commit()
		return new_entry.id
	except Exception as e:
		db.session.rollback()
		print(f"Failed to insert record into OutputDataMore: {e}", flush=True)
		raise


def bbcode(summary):
	prompt = "Format this text according to the rules provided in the role:\n"
	prompt += summary
	try:
		response = client.chat.completions.create(model=OPENAI_MODEL,
												  messages=[
													  {"role": "system",
													   "content": 'Format text provided in the prompt according to these rules:\n1. Wrap each product name (including its color after the dash) in [b]...[/b] tags. Product names do NOT contain SKU codes.\n2. Structure each entry as a BBCODE list item within [list][*]...[/list] tags.\n3. Retain all original text and punctuation but apply the formatting as specified. Do NOT add, remove, or rephrase any content.\n4. Replace double quotation marks with single quotation marks.\n5. Keep line breaks as they are.\n6. Always use these paragraph titles in this specific order (words only, no punctuation): FREQUENT RETURN REASONS, SIZE ISSUES, OTHER CONSIDERATIONS, COMMON ISSUES FOR SPECIFIC PRODUCT LABELS, NOTEWORTHY PRODUCTS.\nExample of expected formatting:\nINPUT:\nNoteworthy Products:\n- Muscle Fit Dress Shirt - White: Notorious for inconsistent sizing, both too tight and too loose.\n- Muscle Fit T-Shirt - White: Criticized for poor quality and size issues.\n- Muscle Fit Essential Trousers - Charcoal: Returns center on tightness in the thigh area.\nEXPECTED OUTPUT:\nNOTEWORTHY PRODUCTS:[list][*][b]Muscle Fit Dress Shirt - White:[/b] Notorious for inconsistent sizing, both too tight and too loose.[*][b]Muscle Fit T-Shirt - White:[/b] Criticized for poor quality and size issues.[*][b]Muscle Fit Essential Trousers - Charcoal:[/b] Returns center on tightness in the thigh area.[/list]\n\nKeep the paragraph names capitalized as titles before each section. Do not wrap paragraph titles in BBCODE. Each paragraph must have its own separate [list][/list].'},
													  {"role": "user", "content": prompt}
												  ])
		return response.choices[0].message.content
	except Exception as e:
		print(f"An error occurred: {e}")
		return None


def build_combined_prompt(data, trim_level):
	"""Build a single prompt that includes both structured return counts and customer comments."""
	data = data.copy()
	expected_cols = ['Product_Name', 'Variance_Title', 'Product_Tags', 'SKU', 'Return_Reason', 'Sub_Reason', 'Comment']
	for col in expected_cols:
		if col not in data.columns:
			data[col] = ""
		data[col] = data[col].astype(str).str.strip()
	if 'Sub_Reason' in data.columns:
		data['Sub_Reason'] = data['Sub_Reason'].replace('nan', 'Unknown')

	prompt = "Based on the following return data, identify key trends, common return reasons, and noteworthy products.\n\n"
	prompt += "Note: each row is one return. The same product may appear many times with different SKUs (which represent size/color variants). Analyse at product level, not variant level.\n\n"

	# PART 1: Collapsed return counts (quantitative)
	prompt += "=== SECTION 1: RETURN COUNTS (each line is a unique product/reason combination with its return count) ===\n"
	if trim_level == 0:
		prompt += "Format: Product name;;SKU;;Product labels;;Return Reason;;Sub Reason;;Count\n\n"
	elif trim_level == 1:
		prompt += "Format: Product name;;SKU;;Return Reason;;Sub Reason;;Count\n\n"
	else:
		prompt += "Format: Product name;;Return Reason;;Sub Reason;;Count\n\n"

	collapsed_data = data.groupby(
		['Product_Name', 'Variance_Title', 'Product_Tags', 'SKU', 'Return_Reason', 'Sub_Reason']
	).size().reset_index(name='Duplicate_Count')
	print(f"Number of rows in collapsed_data: {collapsed_data.shape[0]}")

	for _, row in collapsed_data.iterrows():
		if trim_level == 0:
			prompt += f"{row['Product_Name']};;{row['SKU']};;{row['Product_Tags']};;{row['Return_Reason']};;{row['Sub_Reason']};;{row['Duplicate_Count']}\n"
		elif trim_level == 1:
			prompt += f"{row['Product_Name']};;{row['SKU']};;{row['Return_Reason']};;{row['Sub_Reason']};;{row['Duplicate_Count']}\n"
		else:
			prompt += f"{row['Product_Name']};;{row['Return_Reason']};;{row['Sub_Reason']};;{row['Duplicate_Count']}\n"

	# PART 2: Customer comments (qualitative)
	prompt += "\n=== SECTION 2: CUSTOMER COMMENTS (verbatim feedback from returns that included a comment) ===\n"
	if trim_level == 0:
		prompt += "Format: Product name;;Size;;SKU;;Return Reason;;Comment\n\n"
	elif trim_level == 1:
		prompt += "Format: Product name;;SKU;;Return Reason;;Comment\n\n"
	else:
		prompt += "Format: Product name;;Return Reason;;Comment\n\n"

	comment_count = 0
	for _, row in data.iterrows():
		comment = row.get('Comment', '')
		if pd.isna(comment) or str(comment).strip() == "" or comment == "nan":
			continue
		comment_count += 1
		if trim_level == 0:
			prompt += f"{row['Product_Name']};;{row['Variance_Title']};;{row['SKU']};;{row['Return_Reason']};;{row['Sub_Reason']};;{str(comment).strip()}\n"
		elif trim_level == 1:
			prompt += f"{row['Product_Name']};;{row['SKU']};;{row['Return_Reason']};;{row['Sub_Reason']};;{str(comment).strip()}\n"
		else:
			prompt += f"{row['Product_Name']};;{row['Return_Reason']};;{row['Sub_Reason']};;{str(comment).strip()}\n"
	print(f"Number of comments included: {comment_count}")

	prompt += "\n=== ANALYSIS INSTRUCTIONS ===\n"
	prompt += "Analyse both sections together. Use the return counts (Section 1) to identify volume patterns and the customer comments (Section 2) for qualitative insight.\n"
	prompt += "IMPORTANT CONSTRAINTS:\n"
	prompt += "- Focus on the TOP issues only. Each section should have at most 5-7 bullet points.\n"
	prompt += "- Do NOT list SKU codes. Reference products by name and primary color only (e.g. 'Hayla Denim Pants - Black Wash').\n"
	prompt += "- Do NOT list every variant or color of a product. If multiple colors share the same issue, say 'across multiple colors' instead of listing them all.\n"
	prompt += "- Each bullet point should be 1-2 sentences. Be specific about the problem but concise.\n"
	prompt += "- Only mention products with strong evidence (high return count or repeated consistent complaints). Skip marginal cases.\n"
	prompt += "- Noteworthy Products: limit to the 5-8 worst offenders with a one-sentence summary each.\n"
	prompt += "- Frequent Return Reasons: brief insight about each top reason, not exhaustive product lists.\n"
	prompt += "- Don't use markdown. Each section has to be a bulleted list.\n"
	prompt += "Use this structure: Frequent Return Reasons, Size Issues, Other Considerations, Common Issues for Specific Product Labels, Noteworthy Products."
	return prompt


SYSTEM_PROMPT = (
	"You are a very experienced professional in e-Commerce data analysis. You help a large company understand "
	"user behaviour and inventory issues that lead to order returns.\n\n"
	"CRITICAL RULES:\n"
	"- Be concise and actionable. Focus ONLY on the strongest signals — issues backed by high return volume or "
	"repeated consistent customer comments. Do not list every product or every issue.\n"
	"- When referencing a product, use ONLY the product name and primary color. Do NOT list SKU codes. "
	"Example: 'Gaia Double Breasted Coat - Black' not 'Gaia Double Breasted Coat - Black (COFS007461000104, COFS007471000103, ...)'.\n"
	"- Keep the letter case of product names exactly as in the source data.\n"
	"- Each section should have at most 5-7 bullet points. Each bullet point should be 1-2 sentences max.\n"
	"- Only mention a product if there is strong evidence (multiple returns with the same complaint). "
	"Do not speculate or infer issues that are not clearly supported by the data.\n"
	"- Noteworthy Products section: limit to the top 5-8 most problematic products by volume and severity.\n"
	"- Don't use markdown. Each section must be a bulleted list.\n"
	"- Use this structure: Frequent Return Reasons, Size Issues, Other Considerations, "
	"Common Issues for Specific Product Labels, Noteworthy Products."
)


def call_openai_with_retry(data, trim_levels, model=None):
	if model is None:
		model = OPENAI_MODEL
	for trim_level in trim_levels:
		prompt = build_combined_prompt(data, trim_level)
		try:
			response = client.chat.completions.create(
				model=model,
				messages=[
					{"role": "system", "content": SYSTEM_PROMPT},
					{"role": "user", "content": prompt},
				]
			)
			print(f"Success with trim_level={trim_level}.")
			return response.choices[0].message.content
		except Exception as e:
			print(f"Error with trim_level={trim_level}: {e}")
		print(f"Failed with trim_level={trim_level}.")
	return None


def process_file_thread(request_id, store, month, year, file_content, original_filename):
	print(f"Successfully starting thread for request_id: {request_id}\n")
	print(f"Processing file: {original_filename} for store: {store}, month: {month}, year: {year}")
	with app.app_context():
		print("Entered app context...", flush=True)
		try:
			file_content.seek(0)
			print(f"Reading CSV file: {original_filename}", flush=True)
			data = pd.read_csv(file_content)

			print("ANALYSIS:", flush=True)
			summary = call_openai_with_retry(data, [0, 1, 2])
			if not summary:
				print(f"Failed to generate analysis for {original_filename}", flush=True)
				raise Exception("Failed to generate analysis")
			print(summary)

			print("==========")
			print("FORMATTED SUMMARY:")
			print("Applying BBCode formatting...")
			bbformatted = bbcode(summary)
			if not bbformatted:
				raise Exception("Failed to apply BBCode formatting")

			sections = parse_text_to_sections(bbformatted)
			status = "success"

			new_id = insert_parsed_data_async(
				request_id=request_id,
				status=status,
				original_filename=original_filename,
				store=store,
				month=month,
				year=year,
				parsed_data=sections
			)
			print(f"Successfully processed file. Database ID: {new_id}")

		except Exception as e:
			print(f"Error processing file {original_filename}: {str(e)}")
			status = "error"
			error_sections = {
				"FREQUENT RETURN REASONS": f"Processing error: {str(e)}",
				"SIZE ISSUES": None,
				"OTHER CONSIDERATIONS": None,
				"COMMON ISSUES FOR SPECIFIC PRODUCT LABELS": None,
				"NOTEWORTHY PRODUCTS": None
			}
			insert_parsed_data_async(
				request_id=request_id,
				status=status,
				original_filename=original_filename,
				store=store,
				month=month,
				year=year,
				parsed_data=error_sections
			)

app = create_app()

@app.route("/")
def hello():
	return "Hello, World!"

@app.route("/debug")
def serve_debug_page():
	base_dir = os.path.dirname(os.path.abspath(__file__))
	return send_from_directory(base_dir, "debug_page.html")

@app.route('/test-auth')
@basic_auth.required
def test_auth():
	return "Authenticated!"

@app.errorhandler(413)
def request_entity_too_large(error):
	return jsonify({"error": "File size exceeds the maximum limit"}), 413

@app.route('/read/entry/<request_id>', methods=['GET'])
def get_entry_by_id(request_id):
	if not request_id:
		return jsonify({"error": "id query parameter is required"}), 400

	entry = OutputDataMore.query.filter_by(request_id=request_id).first()

	if not entry:
		return jsonify({"error": f"No entry found for request_id: {request_id}"}), 404

	response_data = OrderedDict([
		("request_id", entry.request_id),
		("status", entry.status),
		("store", entry.store),
		("month", entry.month),
		("year", entry.year),
		("frequent_return_reasons", entry.frequent_return_reasons),
		("size_issues", entry.size_issues),
		("other_considerations", entry.other_considerations),
		("label_issues", entry.label_issues),
		("noteworthy_products", entry.noteworthy_products),
	])
	response_json = json.dumps(response_data)
	return Response(response_json, content_type='application/json')

@app.route('/process/async', methods=['POST'])
def process_csv_async():
	request_id = str(uuid.uuid4())
	store = request.form.get("store")
	month = request.form.get("month")
	year = request.form.get("year")

	if not all([store, month, year]):
		return jsonify({"error": "store, month, and year are required"}), 400

	if 'file' not in request.files:
		return jsonify({"error": "No file provided"}), 400

	file = request.files['file']
	if not file or file.filename == '':
		return jsonify({"error": "No file selected"}), 400

	if not file.filename.lower().endswith('.csv'):
		return jsonify({"error": "Only CSV files are accepted"}), 400

	file_content = BytesIO(file.read())
	original_filename = file.filename

	thread = threading.Thread(
		target=process_file_thread,
		args=(request_id, store, month, year, file_content, original_filename)
	)
	thread.daemon = True
	thread.start()

	return jsonify({"status": "Success", "request_id": request_id}), 202


@app.route("/process/sync", methods=["POST"])
def process_csv():
	request_id = str(uuid.uuid4())
	store = request.form.get("store")
	month = request.form.get("month")
	year = request.form.get("year")
	if 'file' not in request.files:
		return jsonify({"error": "No file provided"}), 400
	file = request.files.get('file')
	if not file or not file.filename.lower().endswith('.csv'):
		return jsonify({"error": "Only CSV files are accepted"}), 400
	original_filename = file.filename
	try:
		data = pd.read_csv(file)

		print("ANALYSIS:")
		summary = call_openai_with_retry(data, [0, 1, 2])
		if not summary:
			raise Exception("Failed to generate analysis")

		print("FORMATTED SUMMARY:")
		bbformatted = bbcode(summary)
		if not bbformatted:
			raise Exception("Failed to apply BBCode formatting")

		sections = parse_text_to_sections(bbformatted)
		status = "success" if sections else "failed"
		new_id = insert_parsed_data_async(
			request_id=request_id,
			status=status,
			original_filename=original_filename,
			store=store,
			month=month,
			year=year,
			parsed_data=sections
		)
		return jsonify({"status": status, "store": store, "month": month, "year": year, "database_id": new_id, "request_id": request_id, "output": sections}), 200

	except Exception as e:
		return jsonify({"error": f"Failed to process file: {e}"}), 500


@app.route('/process/status/<request_id>', methods=['GET'])
def get_processing_status(request_id):
	result = OutputDataMore.query.filter_by(request_id=request_id).first()
	if not result:
		return jsonify({"status": "not_found"}), 404

	response = {
		"request_id": result.request_id,
		"status": result.status,
		"store": result.store,
		"month": result.month,
		"year": result.year
	}

	return jsonify(response)

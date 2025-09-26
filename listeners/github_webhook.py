
@app.route('/webhook', methods=['POST'])
def handle_webhook():
    payload = request.json
    
    if payload['action'] == 'moved':
        # Card moved on Kanban board
        project_name = identify_project(payload['project']['id'])
        column = payload['project_card']['column_name']
        
        if column == 'Requirements Analysis':
            # Trigger BA agent on the local project clone
            trigger_pipeline(
                project_name=project_name,
                stage='business_analyst',
                context={'issue': payload['issue']}
            )
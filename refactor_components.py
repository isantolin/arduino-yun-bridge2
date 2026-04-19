import os
import glob

def main():
    directory = 'mcubridge/mcubridge/services'
    files = glob.glob(os.path.join(directory, '*.py'))
    
    for filepath in files:
        with open(filepath, 'r') as f:
            content = f.read()
            
        modified = False
        
        if 'self.state.publish(' in content:
            content = content.replace('self.state.publish(', 'self.ctx.mqtt_flow.publish(')
            modified = True
            
        if 'self.state.enqueue_mqtt(' in content:
            content = content.replace('self.state.enqueue_mqtt(', 'self.ctx.mqtt_flow.enqueue_mqtt(')
            modified = True
            
        if modified:
            with open(filepath, 'w') as f:
                f.write(content)

if __name__ == "__main__":
    main()
